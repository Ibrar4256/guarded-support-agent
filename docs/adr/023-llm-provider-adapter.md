# ADR-023: The LLM Provider Adapter — Client, Streaming, Failures Under a Deadline, and Cost Records

**Status:** Accepted (2026-11-14). Revised 2026-11-14 after one outside review: no retry once
text has gone to the callback, and the callback is isolated (Part 2); input-driven 400s (context
too long, input rejected) are their own errors and don't page (Part 3, S1); 429s are classified
per provider, an unclassifiable one is a rate limit (Part 3); partial usage on failed attempts,
character counts on every row (Part 4); a fallback model is evaluated before it's enabled
(Part 3).

**Locked 2026-11-14.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Precedence:** fills in ADR-005's "port Project 1's provider, retry and telemetry pieces to
`AsyncOpenAI`" and ADR-022 S8 (what the turn needs from the provider). ADR-006 (ScriptedLLM, the
contract test, real-provider fixtures, the production gate) applies unchanged: the adapter is
the "real adapter" it tests against. Delivering streamed text to the customer stays with the
chat API's streaming ADR.

## Context
Settled already:
- **ADR-022 S8:** the turn makes one call with messages, tool schemas and a deadline, and gets a
  normalized response (text, tool calls with raw arguments, finish reason, token counts) or one
  of a closed set of errors; the adapter never retries past the deadline. Every provider call
  counts toward `TURN_MAX_MODEL_CALLS`; the adapter's retries inside one call count as one.
  A provider failure becomes the turn's `try_again` reply (ADR-022 Part 2).
- **ADR-006:** `ScriptedLLM` implements the same interface; its text turns emit chunks; the
  contract test runs the same scenarios (a tool call, text, streamed text, a 429, a timeout,
  malformed arguments, an empty reply, several tool calls in one response) through both and
  requires the same normalized objects and exception types; at least one captured fixture per
  real provider; a two-layer production gate.
- **CLAUDE.md:** free-tier providers (Groq, Gemini) behind a provider-agnostic interface;
  retry/backoff for free-tier rate limits; every call logged with tokens, cost and latency, so
  "what do 1,000 users cost per month" has an answer.
- **ADR-005:** async everywhere; `AsyncOpenAI`.

What Project 1 actually offers (checked): a sync `OpenAI` client with a base-URL table per
provider, a retry that handles only Gemini's own exception types and sleeps, no streaming, and a
JSONL telemetry writer with a per-model price dict. The base-URL table and the telemetry record's
shape carry over; the rest is new code. "Port" overstates it, and `KNOWN_TRADEOFFS.md`'s
code-reuse entry is corrected with the build.

Not settled:
1. **The client underneath** (ADR-005 named it; the alternatives are recorded here).
2. ★ **Whether the adapter streams now**, before anything delivers tokens to a customer.
3. ★ **Failures:** how retries fit inside a deadline, and whether a second provider is used.
4. ★ **Where call records and costs live.**

## Options Considered

### Part 1 — The client underneath

#### Option A: The `openai` SDK's `AsyncOpenAI` against each provider's OpenAI-compatible endpoint — CHOSEN
- How it works: one `AsyncOpenAI` per configured provider (base URL, key, model), with the SDK's
  own retries turned off (`max_retries=0`) so the adapter's deadline-bounded retry is the only
  one. Provider differences (finish reasons, tool-call id formats, usage in streams, error
  bodies) are normalized in one module and pinned by captured fixtures (ADR-006).
- Pros: what ADR-005 chose; typed streaming events and error classes; Groq and Gemini both
  offer OpenAI-compatible endpoints, so one code path serves both; respx mocks it (ADR-006 D).
- Cons: compatibility layers differ in the details and change without notice (the fixtures and
  the nightly smoke set exist for exactly this); a new runtime dependency.
- Cost/latency/complexity profile: one dependency; one adapter module plus a normalization
  module.

#### Option B: Plain `httpx` (already a dependency) speaking the wire format — rejected
- How it works: hand-written requests and a hand-written server-sent-events parser.
- Pros: no new dependency; full control of timeouts and streaming, as the refund client has.
- Cons: re-implements stream parsing, tool-call delta assembly and error mapping the SDK
  already does; supersedes ADR-005 for little gain.
- When it WOULD be the better choice: a single provider with a small, stable API surface, or a
  dependency budget that rules the SDK out.

#### Option C: LiteLLM (or a similar multi-provider router) — rejected
- Pros: one call shape for many providers; built-in cost tables and fallbacks.
- Cons: a large dependency with its own retries, cost tables and fallback logic, each of which
  would have to be turned off or trusted; the normalization this project needs to test is hidden
  inside it.
- When it WOULD be the better choice: many providers, or a team that wants routing as a product,
  not as code it owns.

#### Option D: Each provider's native SDK — rejected
- Pros: the best fidelity per provider.
- Cons: two code paths, two sets of types, two sets of contract tests.
- When it WOULD be the better choice: a provider feature the compatible endpoint lacks becomes
  essential.

### Part 2 — Streaming in the adapter

#### Option A: The adapter always streams from the provider, assembles the full response, and offers an optional text-delta callback — CHOSEN
- How it works: every call uses the provider's streaming mode and asks for usage in the stream.
  The adapter assembles text and tool-call deltas into the same normalized response a
  non-streaming call would return; if the caller passes `on_text`, text deltas go to it as they
  arrive. The turn (ADR-022) ignores the callback until the chat API's streaming ADR wires it.
  - **No retry after delivery:** once `on_text` has received a delta, a dropped stream fails the
    call (the turn gives its `try_again`) instead of retrying, because a retry would stream a
    second answer after the partial first one. Streams that have delivered no text (tool calls
    only, or nothing yet) can still be retried. What the customer's screen does with the partial
    text is the streaming ADR's job; the adapter's rule exists now so that wiring the callback
    later changes no adapter code.
  - **The callback is isolated:** an exception from `on_text` (a client that disconnected) is
    logged, the callback is switched off for the rest of the call, and assembly continues. It is
    never a provider failure and never causes a retry.
- Pros: one code path, so the contract test covers what production runs (ADR-006 already
  requires a streamed scenario); time to first token is measurable from day one; adding
  delivery later changes no adapter code.
- Cons: tool-call deltas are the hardest part to normalize and providers differ there; a
  provider that omits usage from streams makes the call "uncounted" (ADR-006 rule 4: never a
  silent zero), so cost reporting depends on a fixture showing usage arrives.
- Cost/latency/complexity profile: the assembly code (tested against fixtures).

#### Option B: Non-streaming now, streaming added with the chat API — rejected
- Pros: simpler today.
- Cons: a second code path later, and the contract tests and fixtures redone for it; until then
  the tests exercise a path production won't use once streaming lands.
- When it WOULD be the better choice: a product that never streams (batch jobs, email).

#### Option C: Stream only text replies, non-streaming for tool calls — rejected
- Why we didn't use it here: whether a response is text or tool calls isn't known until it
  arrives.

### Part 3 — Failures under a deadline, and a second provider

#### Option A: Retries bounded by the caller's deadline; an optional second provider chosen only at turn start — CHOSEN
- How it works:
  - **Inside one call:** a rate limit, a timeout, a 5xx or a dropped stream that hasn't
    delivered text (Part 2) is retried with
    capped exponential backoff and full jitter, at most `LLM_MAX_ATTEMPTS`, and only while the
    time left before the deadline is at least `LLM_MIN_ATTEMPT_SECONDS`. A `Retry-After` that
    doesn't fit in what's left fails the call now instead of sleeping into a certain failure.
    The whole call, stream included, runs under `asyncio.timeout(deadline)`: cancelling an HTTP
    stream just closes it, unlike cancelling a database statement (ADR-020).
  - **Telling a rate limit from a spent quota:** both usually arrive as 429, and the difference
    is in the body or headers, which differ by provider. Each provider's classifier is pinned by
    a fixture for each kind of 429. Capturing a spent-quota 429 means spending a day's quota, so
    that fixture may be the provider's documented error body, labeled `source: documented`
    rather than captured. A 429 the classifier can't place is a **rate limit**: retried within
    the deadline, never a cooldown. Misreading a per-minute limit as a spent quota would bench
    the provider for the cooldown; the reverse only costs retries, and repeated failures show up
    through ADR-022's provider-failure alert.
  - **Never retried:** quota exhaustion (a daily or monthly limit), authentication, and every
    400. Quota and auth put that provider into a cooldown (`LLM_QUOTA_COOLDOWN_SECONDS`) and
    raise an alert. A 400 is classified by what caused it:
    - the context is too long (`ContextTooLong`: ADR-022's character limits only approximate
      tokens) → the turn's `try_again` and an alert, no page;
    - the provider's safety filter rejected the input (`InputRejected`) → a `rephrase` template
      ("I can't help with that message as written. Could you put it another way?"), the run stays
      in `AGENT_STEP`, and a rate-limited alert. Not an escalation: that would let anyone hand
      their conversation to a person by typing something the filter blocks; real legal threats
      and abuse are C01's to escalate, and C01 has already run on the message;
    - anything else is `InvalidRequest`: a request the adapter built wrongly (a malformed body,
      an unknown parameter). That's a bug, so it pages (rate-limited like every page).
    Each mapping is pinned by a fixture per provider; a 400 that can't be classified is
    `InvalidRequest`.
  - **A second provider** (`LLM_FALLBACK_*`, optional): a turn picks its provider when it
    starts, the primary unless it's cooling down, and keeps it for the whole turn. A turn never
    switches mid-loop. With no fallback configured this is plain single-provider retry.
  - Evals pin one provider (fallback off), so reported numbers never mix models. **A fallback is
    configured only after its model has passed the nightly smoke set and the adversarial suite
    on its own**: the write gate keeps money safe on any model, but injection resistance and
    answer quality aren't known for a model that was never evaluated.
- Pros: a daily free-tier limit doesn't take the demo down for the rest of the day; one model
  sees one turn's transcript; the deadline holds whatever the provider says.
- Cons: two prompts to keep working on two models; the cooldown is per process (another
  process finds out on its own first failure); a conversation can switch models between turns.
- Cost/latency/complexity profile: a retry loop, a per-process cooldown map, one alert kind.

#### Option B: One provider, retries only, no fallback — rejected as the default (it's A with no fallback configured)
- Pros: one model, simpler prompts.
- Cons: a spent daily quota means `try_again` for every turn until it resets.
- When it WOULD be the better choice: a paid tier with real rate limits and an SLA.

#### Option C: Fail over mid-turn, inside the call — rejected
- Pros: the turn finishes even when the primary fails halfway.
- Cons: the second model inherits the first one's tool-call ids and transcript format, which
  differ between providers; a turn's behavior becomes a mix of two models.
- When it WOULD be the better choice: stateless single calls (classification, summaries) where
  there's no transcript to hand over. The summary task (ADR-022 S6) may use it later.

#### Option D: A client-side rate limiter (token bucket) instead of retries — rejected
- Pros: stays under the provider's per-minute limit instead of hitting it.
- Cons: per process only, and the provider's limit is per key across processes; the real
  limit isn't published reliably.
- When it WOULD be the better choice: one process per key with a documented, stable limit.

### Part 4 — Where call records and costs live

#### Option A: An append-only `llm_calls` table plus one structured log line per call — CHOSEN
- How it works: after every call (success or failure, retries recorded as attempts of one call),
  a row: run, turn, step, purpose (`turn` or `summary`), provider, model, attempts, outcome,
  input and output tokens (NULL when uncounted), how complete that usage is (`full`; `partial`
  when an earlier attempt failed after its stream had started, since a provider may bill tokens
  generated before a drop; `none`), prompt and output character counts (facts, always recorded),
  time to first token, total latency, list-price cost (NULL when the model is unpriced), the
  price-table version. A cost total that includes a `partial` row is reported as a lower bound.
  If a provider never sends usage, a token estimate is computed in the report from the character
  counts and labeled an estimate; no estimate is stored as if it were measured. Written in its
  own short transaction, **not fenced by the lease**: a lost turn's calls still cost money.
  Best-effort: a failed insert is logged, never fails the turn. The runtime role can insert, not
  update. No prompt or response text in the row; message text already lives in `events`, and
  prompt content belongs to the observability ADR.
- Prices come from a committed price table (`model → input and output price per million
  tokens`, with the date and source page), versioned; a model missing from it is unpriced,
  never free. Free-tier calls are recorded at list price, so the 1,000-users answer is what the
  traffic would cost on a paid tier.
- Pros: cost per ticket, per conversation and per day is one SQL query; Project 5 can ingest
  the table; failures and retries are counted, not just successes.
- Cons: one insert per model call; prices go stale (the version and date show how stale).
- Cost/latency/complexity profile: a table, a migration, a JSON price file.

#### Option B: Structured logs only (Project 1's JSONL) — rejected
- Pros: no table.
- Cons: answering a cost question means a log pipeline; logs rotate.
- When it WOULD be the better choice: a service already shipping logs to a queryable store.

#### Option C: OpenTelemetry spans (GenAI conventions) now — rejected for now
- Pros: the standard shape; Project 5 can consume it.
- Cons: needs a collector and a backend before anything is queryable; that's Project 5's job.
- When it WOULD be the better choice: once Project 5 exists. The table maps onto spans then.

#### Option D: A hosted tracer (Langfuse, Helicone) — rejected
- Why we didn't use it here: an external service in the data path for something Project 5
  builds; prompts would leave the system.

### Smaller decisions
- **S1. The interface** (one method): `complete(messages, tools, *, deadline, purpose,
  on_text=None) -> ModelResponse`. `ModelResponse`: text, tool calls (id, name, raw argument
  string, never parsed here), finish reason (normalized: `stop`, `tool_calls`, `length`,
  `content_filter`, `other`), usage (or uncounted) and its completeness, provider, model,
  attempts. Errors: `RateLimited`, `ProviderTimeout`, `ProviderUnavailable`, `QuotaExhausted`,
  `AuthFailed`, `ContextTooLong`, `InputRejected`, `InvalidRequest`. Argument JSON is parsed by
  the turn, where a parse failure is malformed (ADR-022 S1).
- **S1a. How the turn treats them** (filling in ADR-022 Part 2, whose list named rate limits,
  timeouts, 5xx and quota): `RateLimited`, `ProviderTimeout`, `ProviderUnavailable`,
  `QuotaExhausted`, `AuthFailed` and `ContextTooLong` → `try_again` with cause `provider`;
  `InputRejected` → `rephrase`; `InvalidRequest` → `try_again` with cause `provider` and a page.
- **S2. A truncated response** (`length`) is malformed for the turn when it carries tool calls
  (the arguments may be cut off) and a reply when it's text only (the text is kept, the
  truncation is recorded). `content_filter` is a reply only if it has text; otherwise
  malformed.
- **S3. Settings**, from the environment and validated at startup: `LLM_PROVIDER` (`groq`,
  `gemini`, `scripted`), `LLM_MODEL`, `LLM_API_KEY`, optional `LLM_FALLBACK_PROVIDER`,
  `LLM_FALLBACK_MODEL`, `LLM_FALLBACK_API_KEY`; `LLM_MAX_OUTPUT_TOKENS` (1024),
  `LLM_TEMPERATURE` (0.2), `LLM_MAX_ATTEMPTS` (3), `LLM_BACKOFF_BASE_SECONDS` (0.5),
  `LLM_BACKOFF_CAP_SECONDS` (8), `LLM_MIN_ATTEMPT_SECONDS` (5), `LLM_QUOTA_COOLDOWN_SECONDS`
  (900). Base URLs come from a table in code (Project 1's), not the environment, so a typo can't
  send prompts elsewhere. Model names come from the environment, never hardcoded. Defaults are
  starting values, not measurements.
- **S4. The production gate** is ADR-006's: `scripted` needs `TESTING=true`, and the service
  refuses to start in production with either set.
- **S5. Tokens:** the adapter doesn't count tokens before a call; providers report usage after.
  ADR-022 S9's character limits stay; the `llm_calls` rows give the measured characters per
  token to tune them against.
- **S6. Output bound:** `LLM_MAX_OUTPUT_TOKENS` caps every call, and the assembled response is
  capped in characters too, so a provider ignoring the limit can't stream without end.
- **S7. Keys** live in `.env` and GitHub secrets, set by the user; the adapter logs the provider
  and model, never the key or the request body.

## Decision
Part 1 A, Part 2 A, Part 3 A, Part 4 A, S1–S7.

### Tests (each with a negative control)
| Test | Negative control |
|---|---|
| ADR-006's minimum scenarios give identical normalized objects and exception types through ScriptedLLM and the adapter (respx) | A normalization that drops the finish reason makes the two disagree |
| Captured Groq and Gemini fixtures (a tool call, text, streamed text, an error body) parse to the expected objects | Hand-editing a fixture's tool-call delta breaks the assembly assertion |
| Streamed tool-call deltas split across chunks assemble into the same call as the unsplit form | Concatenating arguments per chunk without the index mixes two calls |
| `on_text` receives the deltas in order, and their join equals the response text | — |
| The stream drops after two text deltas: the call fails and isn't retried | Retrying sends the deltas a second time |
| A stream that drops before any text (tool-call deltas only) is retried | — |
| `on_text` raising: assembly continues, the callback is off, the call succeeds, no retry | Letting the exception out makes it a retried provider failure |
| A stream without usage gives an uncounted response, never zero tokens | Defaulting usage to zero passes the cost assertion it must fail |
| A 429 then success: two attempts, one response, one `llm_calls` row with `attempts = 2` | Recording each attempt as a call counts two calls |
| A `Retry-After` longer than the time left fails at once, without sleeping | Sleeping first overruns the deadline (fake clock) |
| No retry starts with less than `LLM_MIN_ATTEMPT_SECONDS` left, and the whole call ends by the deadline even mid-stream | Dropping the `asyncio.timeout` lets a stalled stream run past it |
| The SDK's own retries are off (`max_retries = 0`) | Leaving the SDK default makes one adapter attempt hit the mock three times |
| Quota exhaustion: not retried, the provider cools down, an alert; the next turn uses the fallback; a turn never switches provider mid-loop | Retrying a quota error hits the mock again |
| Each provider's 429 fixtures classify as rate limit or spent quota; an unknown 429 body is a rate limit, retried, with no cooldown | Defaulting unknown 429s to quota benches the provider |
| A context-too-long 400 → `ContextTooLong`, no page; an input-rejected 400 → `InputRejected`, the turn's `rephrase` reply, a rate-limited alert, no page, no escalation | Mapping every 400 to `InvalidRequest` pages on a customer's message |
| A failed attempt after its stream started, then success: the row's usage is `partial`, and a cost total over it is labeled a lower bound | Recording only the last attempt's usage as `full` under-reports |
| With no fallback configured, a cooling-down primary raises `QuotaExhausted` (the turn's `try_again`) | — |
| A request-shape 400 (`InvalidRequest`) pages and isn't retried | — |
| `llm_calls` row per call with tokens, latencies and list-price cost; an unpriced model gives NULL cost; a lost turn's call is still recorded | Fencing the insert on the lease loses the lost turn's row |
| The runtime role can insert into `llm_calls` and can't update it | Granting `UPDATE` lets a row be rewritten |
| No key or request text appears in any log line or `llm_calls` row | Logging the request at debug shows the key |
| Settings: a fallback missing its key, a min-attempt time above the turn deadline, or an unknown provider name is refused at startup | Removing the check starts with it |

## Consequences
- New: the `openai` dependency (needs approval); `agent/llm/` (interface, adapter,
  normalization, retry, prices), `ScriptedLLM` in `testing/` behind the same interface, an
  `llm_calls` table and migration, a committed price file, `.env.example` entries, fixtures
  captured from real Groq and Gemini responses (needs the user's keys, added by the user).
- `KNOWN_TRADEOFFS.md`: Project 1's LLM layer was mostly rewritten, not ported; per-process
  cooldowns; a conversation can change models between turns; free-tier calls recorded at list
  price; prices go stale; uncounted calls when a provider omits usage, and cost totals as lower
  bounds when an attempt failed mid-stream; a spent-quota fixture may be documented rather than
  captured; a dropped stream after visible text fails the turn rather than retrying.
- A new reply template, `rephrase`, beside ADR-022's `try_again`.
- **Revisit if:** a provider's compatible endpoint drifts from the fixtures more than once (move
  that provider to its native SDK, Option D); several processes keep hitting the same quota
  (share the cooldown through the database); Project 5 is ready to take spans (map `llm_calls`
  onto them).

## Interview-ready summary
"The adapter is one method: messages, tools and a deadline in, a normalized response or one of a
few typed errors out. It always streams from the provider and assembles the result, so the code
my tests exercise is the code production runs, and streaming to the customer later is just a
callback. Retries live inside the caller's deadline: if the provider says retry in thirty
seconds and I have ten left, I fail now instead of sleeping into a certain timeout, and the
SDK's own retries are off so there's only one retry policy. A spent free-tier quota puts that
provider in a cooldown and the next turn starts on the fallback, but a turn never switches
models halfway, because the transcript format differs between providers. Errors are classified
by cause: a 400 because the conversation got too long, or because the provider's filter rejected
a customer's message, isn't a bug, so it doesn't page; otherwise any customer could wake someone
up by typing something rude. And once text has streamed to the customer, a dropped stream fails
instead of retrying, so nobody sees the answer start twice. Every call, including failed ones
and calls from a turn that lost its lease, becomes a row with tokens, latency and list-price
cost, and a model missing from the price table is unpriced, never free. That's how I can answer
what a thousand users would cost."

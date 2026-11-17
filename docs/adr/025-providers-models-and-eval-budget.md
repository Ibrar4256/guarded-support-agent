# ADR-025: Providers, Models per Purpose, Record-and-Replay, and an Eval Budget on Free Tiers

**Status:** Accepted (2026-11-17). Revised the same day after two outside reviews: deterministic
eval prompts (S6), a demo reserve, a merge gate and replay-is-not-measurement (S7), verification
tied to a prompt fingerprint and context size (S3, S12), a zero-spend guard (S5), data terms on
free tiers (S11).

**Locked 2026-11-17.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Supersedes in part:** ADR-023 S3's settings shape (one `LLM_PROVIDER`/`LLM_MODEL`/`LLM_API_KEY`
and one optional `LLM_FALLBACK_*`) and Part 3's limit of one fallback: providers get one
credential set each, and each purpose (the turn, the summary) gets an ordered list of models.
Everything else in ADR-023 stands: AsyncOpenAI against OpenAI-compatible endpoints, streaming,
retries inside the caller's deadline, errors classified by cause, one model per turn chosen at its
start, `llm_calls` rows, and "a fallback is configured only after its model has passed the evals",
which this ADR turns from a rule into a startup check. **Follow-up:** query routing (a router
choosing among a purpose's models per turn) is ADR-026, built on this one.

## Context
The project must run, demo and be evaluated on free tiers only: there's no budget for paid
plans. Free limits are tight. Checked 2026-11-17 on the providers' own pages:
- **Groq** (per organization): `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b` at
  30 requests/min, 1,000 requests/day, **8,000 tokens/min**, 200,000 tokens/day.
- **Cloudflare Workers AI:** 10,000 neurons/day ($0.011 per 1,000 neurons beyond it on a paid
  plan). Tool calling is marked "Yes" on the model pages for `@cf/meta/llama-3.3-70b-instruct-fp8-fast`
  (24,000-token context; $0.293/M input, $2.253/M output, so about 26,600 and 204,800 neurons per
  million) and `@cf/openai/gpt-oss-20b` (128,000-token context; $0.20/M input, $0.30/M output, so
  about 18,200 and 27,300 neurons per million). Both pages say `/v1/chat/completions` is supported,
  but no Cloudflare page shows `tools` passed through that endpoint, so it's to be proven by a
  captured response (S4). **Estimated** at 2,500 input and 300 output tokens per call (unmeasured):
  about 130 neurons a call on Llama 3.3 70B (roughly 75 calls, or 25 three-call turns, a day) and
  about 55 on `gpt-oss-20b` (roughly 180 calls, or 60 turns, a day).
- **Gemini:** limits are per project; the per-model free numbers must be read from the project's
  own AI Studio page (the public page didn't render usable numbers when checked).
- **Further one-account candidates:** OpenRouter's `:free` models allow 20 requests/min and 50
  requests/day (1,000/day only after buying credits, which isn't free); NVIDIA NIM's hosted API
  offers free developer access whose limits and terms are not yet verified. Each would go through
  S4 and S5 before use.

EVAL_PLAN's nightly LLM suites (about 30 adversarial cases × 3 runs, 30 resolution tickets, a
smoke set) come to roughly 600–800 model calls a night at an estimated 2–3k input tokens each.
That estimate is unmeasured. No single free account covers it, and some suites can't fit even
across three.

**Not available:** several accounts at one provider. Groq's Acceptable Use Policy forbids going
beyond rate limits "by registering multiple accounts or orchestrating usage between multiple
organizations"; Google applies limits per project and treats extra projects or accounts for more
free quota as circumvention. That holds whether the accounts are the developer's own or
friends', and it would put those accounts at risk of suspension.

**Configuration:** nothing provider- or account-specific is hardcoded. Keys, account ids, which
provider and model serves each purpose, and the order of fallbacks all come from the
environment. Only the hosts that prompts may be sent to stay in code (ADR-023 S3's reason: a typo
mustn't send prompts to another host).

Constraints: zero spend; no reduction in eval coverage (every case still runs k=3 against the
live model when a prompt or model changes); one account per provider; free hosting can't run a
model server.

## Options Considered

### Option A: Several providers, one account each, models per purpose from the environment, record-and-replay for evals, and a budget-aware eval schedule — CHOSEN
- How it works: Groq, Gemini and Cloudflare (each with one account), plus Ollama for local
  development and evals only. Each purpose has an ordered list of `provider:model` entries from
  the environment; the first is the primary, the rest are fallbacks used when the earlier ones
  are cooling down. A model may serve a purpose only if it passed that purpose's evals, recorded
  in a committed results file; production refuses to start otherwise. Evals record each live
  response once and replay it after, so regression runs cost no quota; live runs are scheduled
  inside a token budget and resume across days.
- Pros: three providers' free quota, all within their terms; real fallback to test; the turn
  gets the best model that passes, measured; summaries can go to a provider that needs no tool
  calling; regression evals become reproducible and free; the full k=3 suite still runs live.
- Cons: more to build (per-provider settings, a recording store, a resumable runner); a full live
  eval run can take several days of quota instead of one night; each new provider needs its own
  captured fixtures and error markers.
- Cost/latency/complexity profile: zero spend; replay runs are milliseconds per case; moderate
  build cost.

### Option B: Several accounts per provider, rotated — rejected
- How it works: keys from several personal or friends' accounts at each provider; a rotator
  spreads calls across them.
- Pros: more free quota with no other work.
- Cons: against the providers' terms (Groq's policy names it; Google counts limits per project
  and treats this as circumvention); suspension risk falls on every account involved, including
  friends'; other people's credentials in this project's secrets; and in an interview, "I rotated
  accounts to dodge limits" undermines the rest of the project.
- Why we didn't use it here: it breaks the providers' terms, and Option A meets the bar without it.
- When it WOULD be the better choice: never for free-tier limits. Separate accounts are legitimate
  only for genuinely separate purposes (a work account for work), not pooled behind one app.

### Option C: A local model (Ollama) as the primary — rejected as primary, kept for development and evals
- How it works: run a small open model on the developer's machine for every call.
- Pros: free and unlimited; no terms or quotas; works offline.
- Cons: about 2.9 GB of free disk fits only a 1–3B model; small models are weak at tool calling,
  which every refund turn needs; free hosting (Fly, Render) can't run it, so the deployed app
  couldn't use it; slow on CPU.
- Why we didn't use it here: quality and deployability; it stays as an optional provider for
  local development and eval runs (refused with `APP_ENV=production`).
- When it WOULD be the better choice: a machine with a GPU and disk to spare, or a
  privacy-bound deployment where data can't leave the host.

### Option D: A paid tier with a spending cap — rejected
- How it works: pay-as-you-go on one provider with a hard monthly cap.
- Pros: simplest; higher limits; one model for everything.
- Cons: costs money, which this project doesn't have.
- Why we didn't use it here: zero budget.
- When it WOULD be the better choice: any funded project. Even a small monthly budget changes the
  answer: a pay-as-you-go model with a hard quota cap would remove most of S7's multi-day
  scheduling (the amount is to be computed from measured tokens and current prices, not
  estimated here). Also the one-off frontier comparison CLAUDE.md allows.

### Option E: Run every LLM suite live every night, as EVAL_PLAN first said — rejected
- How it works: the full adversarial and resolution suites at k=3, live, nightly.
- Pros: freshest numbers.
- Cons: doesn't fit free quota (see Context); a suite that fails for quota reasons trains people
  to ignore it.
- Why we didn't use it here: the same coverage is kept by recording when prompts or models change
  and running the full suite live then, spread over days.
- When it WOULD be the better choice: with a paid budget, as a complement to replay.

## Decision
Option A, with these decisions:

- **S1. Settings (all from the environment, validated at startup; keys never logged).**
  - Credentials, one set per provider, each optional: `GROQ_API_KEY`; `GEMINI_API_KEY`;
    `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`; `OLLAMA_BASE_URL`.
  - Models per purpose, ordered (primary first): `LLM_TURN_MODELS` and `LLM_SUMMARY_MODELS`,
    each a comma-separated list of `provider:model`, for example
    `groq:openai/gpt-oss-120b,gemini:<model>`. Model names come only from here.
  - A provider named in a list without its credentials is refused at startup; an unknown provider
    is refused; an empty list is refused.
  - `scripted` stays ADR-006's test-only provider (`scripted:<label>` with `TESTING=true`, refused
    in production).
  - The other `LLM_*` settings (output caps, temperature, attempts, backoff, cooldown) are
    unchanged and apply to every provider.
- **S2. Where prompts may go.** Each provider's host stays in the code table (ADR-023 S3).
  Cloudflare's URL is a template whose account id comes from `CLOUDFLARE_ACCOUNT_ID`, checked
  against Cloudflare's id format. Ollama's base URL comes from `OLLAMA_BASE_URL` but must point at
  a loopback host (`localhost`, `127.0.0.1`, `[::1]`), and Ollama is refused with
  `APP_ENV=production`.
- **S3. A model serves a purpose only after it passes that purpose's evals, with the prompt it
  will actually get.** A committed file, `services/agent/src/agent/llm/verified_models.json`, has
  one entry per `provider:model` and purpose: the **prompt fingerprint** it was evaluated with,
  its context size in tokens (S12), the eval run id and the date. The fingerprint is the SHA-256
  of that purpose's system prompt and tool schemas, **computed from the code at startup**, not a
  hand-bumped version string, so a changed tool description can't slip past a forgotten bump.
  It's eval output, not configuration: the environment still chooses among verified models. With
  `APP_ENV=production`, the service refuses to start if any listed model has no entry for its
  purpose with the current fingerprint; elsewhere it warns. This enforces ADR-023's rule in
  code, and "verified" stops meaning anything the moment the prompt changes, which is intended.
- **S4. Tool calling must be proven per provider and model.** The turn needs tool calls, so a
  `provider:model` can serve the turn only after a captured fixture shows a tool call through its
  OpenAI-compatible endpoint, parsed by our adapter (ADR-006). A model page marking function
  calling "Yes" isn't enough on its own: the endpoint we call must carry it. Until then the model
  can serve summaries only, which use no tools. Cloudflare's two tool-marked models are the first
  to prove.
- **S5. New providers get captured fixtures and error markers** before they're used: the 429s
  (rate limit vs spent quota), auth failure, context too long and a filter rejection, pinned in
  `tests/unit/llm_fixtures` (ADR-023 Part 3). Until a marker is captured, the safe defaults apply
  (an unknown 429 is a rate limit, an unknown 400 is a request bug).
  **Zero spend is guarded, not assumed.** Each account must be on its free plan with no payment
  method that would allow overage: a Cloudflare account on Workers Paid bills past 10,000 neurons,
  and a Gemini project with billing enabled is no longer on free-tier terms. No provider API
  reliably reports this, so it's a step in the setup runbook, re-checked before each deploy;
  the automated part is that each provider's spent-quota response is captured as a fixture and
  classified as `QuotaExhausted`, so running out stops the provider (ADR-023's cooldown and
  alert) instead of being something that could start billing.
- **S6. Record-and-replay for evals.**
  - **Everything that reaches an eval prompt is deterministic.** Eval runs supply, through seams:
    a seeded random source for fence nonces and generated ids (run, conversation, order and
    action ids, including ids the database would otherwise generate), and a fixed clock for the
    eval database's seed and for every date that reaches a prompt (the digest's ticket dates, the
    lookup's delivery date). Production keeps its random nonces and real clock. A fixed nonce
    doesn't weaken the fence in evals: every marker is escaped whatever the nonce (ADR-022 Part 3,
    already tested). This extends ADR-007's rule (time and randomness are injected into `core`)
    to the turn and database layers, for eval runs. Keys hash the real messages; nothing is
    normalized out of them, which would hide genuine prompt changes.
  - A recording's key is the SHA-256 of the canonical JSON of provider, model, the prompt
    fingerprint (S3), messages, tool schemas, temperature, output cap and the run index (1–3 for
    k=3, so each run keeps its own sample). The value is the normalized response the adapter
    returned (text, tool calls, finish reason, usage).
  - `EVAL_LLM_MODE`: `replay` (the default, and the only mode in per-PR CI: a missing recording
    fails with its key and never calls a model), `record` (calls live and writes the recording),
    `live` (calls live and stores nothing: the drift check).
  - A prompt or model change changes every key, so replay fails loudly until the suite is
    re-recorded; stale recordings can't pass silently.
  - Recordings are committed (synthetic data only), so anyone can replay the evals without keys.
    They include planted adversarial content: they're test data and never feed a live prompt.
  - Replay is an eval-runner feature only; the service never reads recordings, and `replay` is
    refused with `APP_ENV=production`.
- **S7. The eval budget and schedule.**
  - Per-PR: deterministic suites plus the LLM suites in `replay` mode, no quota used.
  - Nightly drift check: one shard of the cases (cases hashed into shards; shard = day of the
    year modulo the shard count), k=1, `live`. Every new real failure becomes a scripted
    regression case (ADR-006).
  - A full re-record (every case, k=3, `record`) whenever a prompt fingerprint or a listed model
    changes. The runner stops cleanly at its budget and resumes the same run the next day, so
    coverage never drops, only speed.
  - **The demo keeps a reserve.** Each provider has a daily limit (`EVAL_PROVIDER_DAILY_TOKENS`)
    and a demo reserve (`EVAL_DEMO_RESERVE_TOKENS`), per provider, from the environment. The
    runner's budget for a provider is at most its limit minus the reserve, checked when the runner
    starts (it refuses a budget that would cut into the reserve). Re-records prefer providers
    that aren't that day's production primary.
  - **How a prompt or model change merges.** The PR merges after a gate subset is recorded on the
    branch: the smoke set and a fixed adversarial subset, k=3. The full re-record finishes after
    the merge and is required before the next deploy, which checks it through S3's fingerprint.
  - Budgets are sized from measured tokens (`llm_calls`), not from this ADR's estimate. The first
    step is one smoke run per provider to measure tokens per conversation.
  - Reports state the run's mode, models, k and dates. **Reported adversarial and quality
    numbers come from the recording run**, with its date and models; per-PR replay is a
    regression gate on our code, not a fresh measurement of the models (the same spirit as
    ADR-006: the stub never produces a reported safety or quality number).
- **S8. Choosing the models (a measured comparison, not a guess).** Candidates for each purpose
  run the smoke set and an adversarial subset, recorded. Compared on: tool-call validity, task
  success, injection successes (counts), tokens per conversation, p50/p95 latency, and fit within
  the free limits. Winners and runners-up go into `verified_models.json` and the env lists. No
  model is named in this ADR.
- **S9. CI secrets.** The nightly drift and re-record jobs need one key per provider as GitHub
  Actions secrets (the developer adds them; never pasted in chat). Per-PR CI needs none.
- **S10. Follow-up: query routing (ADR-026).** A router chooses among a purpose's verified models
  per turn, at the turn's start (never mid-turn, ADR-023). Its constraints, to be decided there:
  it may move a turn to a stronger model on risk but never to a weaker one when the message or
  state touches orders, refunds, an open proposal, an escalation or an attack pattern
  (ADR-009's asymmetry principle); routing tables are per environment (Ollama only locally); and
  it gets its own evals (route accuracy, quality per route against always-strongest, cost and
  latency saved, and adversarial cases trying to force a downgrade). It depends on S8's
  per-model measurements.
- **S11. Data on free tiers: synthetic only.** Free tiers' data terms differ from paid ones.
  Gemini's terms for unpaid use say Google uses the content "to provide, improve, and develop
  Google products", that "human reviewers may read, annotate, and process your API input and
  output", and: "Do not submit sensitive, confidential, or personal information to the Unpaid
  Services." OpenRouter's `:free` models are served by upstream providers with their own logging
  and training policies. So: every provider's data terms are checked before use, like its limits
  (S5), and recorded in the runbook; evals and fixtures use synthetic data only; and the deployed
  demo shows a visible notice that it's a demo on free AI tiers and that people mustn't enter
  real personal information. OpenRouter's `:free` models are used, if at all, only after their
  data terms are checked and only for synthetic data.
- **S12. Each model's context must fit its purpose.** `verified_models.json` records each model's
  context size in tokens (from the provider's model page, confirmed by the captured fixtures).
  At startup, the service computes each purpose's largest possible prompt: the system prompt, the
  tool schemas, the digest's maximum, the summary (bounded by the output-token cap), the window
  budget, and the turn's tool results (the call budget times the largest tool result), plus
  `LLM_MAX_OUTPUT_TOKENS`, converted at a conservative 3 characters per token until tokens are
  measured. It refuses to start if any listed model's context is smaller. Measured on 2026-11-17:
  927 characters of system prompt and 2,085 of tool schemas; with a 24,000-character window the
  turn's worst case is about 32,000 characters, about 11,000 tokens plus 1,024 output, which fits
  a 24,000-token model with room, but the check keeps it true as budgets change.

### Tests (each with a negative control)
| Test | Negative control |
|---|---|
| A provider listed in `LLM_TURN_MODELS` without its credentials, an unknown provider, an empty list → startup refused | Removing the check starts with it |
| With `APP_ENV=production`, a listed model not verified for its purpose → startup refused; outside production, a warning | Skipping the verification check starts in production |
| Cloudflare's URL is built from the account id; an account id with a slash or a host in it is refused | Concatenating it unchecked lets `x/../evil` through |
| `OLLAMA_BASE_URL` must be loopback; any other host is refused; Ollama with `APP_ENV=production` is refused | Accepting any host lets a remote URL through |
| A turn uses the first non-cooling model of its purpose's list for the whole turn; a quota error cools that provider and the next turn takes the next entry | A mid-turn switch makes one turn's calls hit two models |
| The summary uses `LLM_SUMMARY_MODELS`, never the turn's list | Using the turn's list fails the recorded-model assertion |
| A `provider:model` without a captured tool-call fixture can't be listed for the turn, even if its model page marks function calling | Removing the check lets it start |
| `replay` with a missing recording fails with the recording's key and makes no network call (strict mock) | Falling back to live makes an unexpected request |
| A changed prompt version or model changes the key | Leaving the prompt version out of the key replays a stale answer |
| k=3 replays three distinct recordings | Dropping the run index from the key replays one sample three times |
| The runner stops at the token budget and resumes the same run without repeating finished cases | Not persisting progress reruns finished cases |
| `replay` and `record` are refused with `APP_ENV=production` | — |
| No key value appears in logs, errors or `repr` | A planted `repr` with the key is caught |
| The same eval case built on two different days, in two processes, gives the same recording key | A live nonce (or the real clock) changes it |
| A changed system prompt or tool description, with `verified_models.json` unchanged → production refuses to start | A hand-maintained version string, not bumped, lets it start |
| A runner budget that would cut into a provider's demo reserve is refused at start | Removing the check starts a run that can exhaust the demo |
| A listed model whose context is below the purpose's largest prompt plus output → startup refused | Removing the check starts, and fallback turns hit `ContextTooLong` |
| A captured spent-quota response is classified `QuotaExhausted`, cooling the provider down | Classifying it as a rate limit keeps retrying the spent account |
| The deployed app's chat page shows the synthetic-data notice | Removing it fails the page test |

## Consequences
- Three providers' free quota, one account each, within every provider's terms.
- Regression evals are reproducible and free; live coverage is unchanged, but a full live run can
  take days.
- More code and fixtures: per-provider settings, Cloudflare and Ollama entries, a recording store,
  a resumable runner, `verified_models.json`.
- Recordings must be re-made whenever a prompt or model changes; that's deliberate (stale
  recordings fail loudly).
- A model can't reach production without passing its evals; switching models is an eval run plus
  an env change, never a code change.
- EVAL_PLAN's "Runs" column changes from nightly to the S7 schedule; EVAL_PLAN gets a pointer line.
- Eval runs need seams for every source of randomness and time that reaches a prompt, including
  ids and timestamps the database would otherwise generate (S6); production behavior is unchanged.
- The deployed demo is synthetic-data-only, with a visible notice (S11); real customer data would
  need paid tiers with different data terms.
- Evals can never take the demo's quota below its reserve, so a re-record is slower but the demo
  stays up (S7).
- **Revisit if:** a budget appears (Option D for the frontier comparison), a provider's free tier
  changes, or the measured tokens per conversation make the schedule too slow.

## Interview-ready summary
"I had to run, demo and evaluate an agent on free tiers only. Instead of pooling accounts, which
the providers' terms forbid, I spread the work across three providers with one account each and
assigned models per job from configuration: the turn needs reliable tool calling, the summary
doesn't, so they don't need the same model. A model can only be used for a job after it passes
that job's evals, and production refuses to start otherwise. For evals I record every live
response once and replay it after, so regression runs are free and reproducible, and when a
prompt or model changes the whole suite is re-recorded at k=3 within a daily token budget,
resuming across days. I kept full coverage and traded only speed. A paid tier would have been
simpler; a local model is unlimited but too weak at tool calling to be the primary and can't run
on free hosting. Query routing comes next, built on the per-model measurements this produces."

# ADR-028: A Context Bound That Holds — Byte Ceilings, Read Budgets, Notes and Summary Chunks

**Status:** Proposed (2026-11-19). Revised the same day after one outside review: the token
estimate is replaced by a measured byte ceiling (Part 0); a spent read budget withdraws the read
tools instead of counting as malformed (Part 1); the cut is applied after escaping (S2); a
summary chunk that keeps failing on its content is skipped with a marker (Part 3); the window's
message count is capped, which also bounds its query (S4). Revised again after a second review:
the failure count lives in its own row (Part 3), a context overflow after this ADR is a bound
violation that alerts (S5), the model lists follow S8 with every remaining model's context
checked, retired setting names are refused (S9), and the one unproven part of the bound is
stated (S10). ADR-027 is reserved for query routing.

**Locked 2026-11-20.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Closes:** the gaps ADR-025 S12 left in the context-fit bound (KNOWN_TRADEOFFS, "The
context-fit bound isn't complete"), and the one it rests on: the fixed 3 characters per token.
**Supersedes in part:** ADR-022 S1's malformed rule (Part 2), the units of ADR-022 S2/S9's
`CUSTOMER_MESSAGE_MAX_CHARS` and `PROMPT_WINDOW_MAX_CHARS` and of ADR-023's
`LLM_MAX_OUTPUT_CHARS` (Part 0), and ADR-022 S6's "a background summary of them" (Part 3).
**Extends** ADR-026: a `script:` field in its case format and an injection step in its recording
wrapper (S7). Pointer lines go into ADR-022, ADR-023, ADR-025 and ADR-026.

## Context

ADR-025 S12 makes every service refuse to start when a listed model's context can't hold its
purpose's largest prompt plus output. `agent/turn/context_fit.py` computes that bound by running
the real formatting code on the largest inputs. Measured today with the `.env.example` defaults:

| Purpose | Bound (tokens) | Parts |
|---|---|---|
| Turn | 17,118 | system prompt and digest 801, summary 1,024, window 8,000, tool schemas 664, earlier responses 5,120, tool results 485, output 1,024 |
| Summary | 14,303 | summary prompt 213, previous summary 1,024, messages to add 12,042, output 1,024 |

The bound is only as good as its assumptions, and four of them are false today:

0. **3 characters make a token.** Every character count above is divided by 3. Captured
   2026-11-19 (`byte_ceiling` fixtures): one customer message of 1,031 characters (a short ASCII
   instruction plus 1,000 four-byte characters: emoji, CJK Extension B, private-use and tag
   characters), 4,031 UTF-8 bytes, no system prompt, no tools:

   | Provider:model | Input tokens (provider's count) | Tokens per character | Tokens per byte |
   |---|---|---|---|
   | groq `openai/gpt-oss-120b` | 3,552 | 3.45 | 0.88 |
   | gemini `gemini-3.5-flash-lite` | 3,510 | 3.40 | 0.87 |
   | cloudflare `@cf/openai/gpt-oss-20b` | 3,550 | 3.44 | 0.88 |
   | openrouter `baidu/ernie-5.5-flash-preview:free` | 1,424 for 431 characters / 1,631 bytes (a smaller payload: the first try hit OpenRouter's shared-pool limit) | 3.30 | 0.87 |

   The estimate predicts 344 tokens; the providers counted about ten times that. The customer
   controls this direction: a 24,000-character window of such text is ~83,000 tokens, not 8,000.
   Per-message overhead (role markers, chat templates) isn't counted either, and during a
   summary outage the window's message count has no limit, so neither does that overhead.
1. **One read result per step.** "Tool results 485" is 5 earlier steps × one 290-character
   `lookup_order` result. But `run_loop` runs *every* read call in a response, and nothing caps
   how many a response holds except the output caps (1,024 tokens, 16,000 characters). A
   minimal call is 32 characters, so the character cap alone allows ~500; the token cap binds
   first, but tokens per call aren't measured. At 50 calls a response, one step adds ~4,800
   tokens of results. Each call is also a database query and a `run_order_lookups` row (a
   duplicate insert is skipped, but the query and the result still happen), and a
   cross-customer id writes a security-log line, so one message could drive hundreds of each.
2. **Corrections are free.** A malformed response is echoed back with an error tool message for
   *every* call, each repeating the first problem, and a problem can quote a model-controlled
   string (`unknown tool 'xxxx…'`, or an unexpected argument key). Measured through the real
   `parse_calls` and `correction` functions:

   | Malformed response | Response size | Correction tool messages |
   |---|---|---|
   | 11 calls, one 100-character unknown name | 403 chars | 2,628 chars |
   | 61 calls, one 4,000-character unknown name | 5,853 chars | 252,528 chars |
   | 51 calls, one 8,000-character unknown name | 9,543 chars | 415,128 chars |

   The echo is quadratic: calls × name length. `TURN_MAX_MALFORMED` (2) allows it twice, and
   empty replies count toward the same budget. The cheap outcome is `ContextTooLong`; the
   expensive one is a provider that accepts the request (Gemini's 250K tokens a minute leaves
   room for it) and burns the free quota the demo depends on (not tried). Write calls are not a
   fan-out: a write call alongside any other call is already malformed and nothing runs, and a
   lone write leaves the loop (ADR-022 S1, `loop.py`).
3. **Summaries keep up.** The summary bound assumes the job holds at most `trigger + 1` (9)
   messages, but `plan_summary` gives the job *every* message beyond the window that no summary
   covers. While summaries fail, that backlog grows by two messages a turn; past the summary
   model's context, every later job fails with `ContextTooLong` and the summary never recovers on
   its own: a failure that keeps itself going.

Constraints: free tiers only (requests a day and tokens a minute are scarce), the startup check
must be a real guarantee, and the turn's contracts hold: nothing in a malformed response runs,
write calls only propose, memory never authorizes.

## Options Considered

### Part 0 — How prompt size is counted

#### Option 0A: A ceiling of one token per UTF-8 byte, with the caps on untrusted text in bytes — CHOSEN
- How it works: every part of a prompt that is text we send is counted at one token per UTF-8
  byte. That's a ceiling, not an estimate, for byte-level tokenizers: byte-level BPE (gpt-oss's
  published tokenizer) and SentencePiece with byte fallback (Gemma's) never emit a token that
  covers less than one byte. Gemini's tokenizer isn't published, so for it, as for every model,
  the evidence is the fixture: the ceiling is checked per model against the provider's own count by a
  `byte_ceiling` fixture (4-byte characters, the worst case), required wherever a tool-call
  proof is (ADR-025 S4): a model without one can't be listed. Text that crosses from one model
  to another (customer messages, the window, replies, the summary) is capped in bytes:
  `CUSTOMER_MESSAGE_MAX_BYTES` (4,000), `PROMPT_WINDOW_MAX_BYTES` (24,000), `LLM_MAX_OUTPUT_BYTES`
  (6,000, replacing the 16,000-character output cap; 1,024 tokens of English is ~4,000–4,500
  bytes, so normal replies never reach it). The model's own earlier responses within a turn
  stay counted at its output-token cap: the same model reads them back with the same
  tokenizer (a stated assumption, S10). Chat-template overhead gets a stated allowance: `REQUEST_OVERHEAD_TOKENS` (256)
  per request plus `MESSAGE_OVERHEAD_TOKENS` (16) per message, now that the message count is
  bounded (S4). These two are stated values, not measured: in the `byte_ceiling` captures the
  template's tokens fit inside the slack of that text (0.87–0.88 tokens per byte), which shows
  nothing about the overhead's size.
- Pros: sound for the measured models, with evidence in CI; the customer can no longer inflate
  tokens per character. ASCII conversations behave exactly as today (a byte is a character).
  No tokenizer libraries, no extra calls.
- Cons: pessimistic for English (~4 bytes a token), so the bound roughly triples: turn ≈ 44K,
  summary ≈ 62K tokens (the exact figures come from `context_fit.py` when built). Models with
  small contexts fail startup (a local 8K–32K Ollama model, and any listed model below ~64K for
  summaries). A CJK customer gets ~1,333 characters a message instead of 4,000.
- Cost/latency/complexity: the units of four settings change; one new fixture kind (built,
  captured for four models); `context_fit.py` counts bytes.

#### Option 0B: Keep 3 characters per token and narrow the claim — rejected
- How it works: the bound stays an estimate, recorded in KNOWN_TRADEOFFS as "in characters at a
  stated ratio".
- Pros: no change; small-context models still pass.
- Cons: the measured ratio is ten times off for text the customer chooses, so the check
  guarantees nothing against an adversary.
- Why we didn't use it here: the startup check exists to be a guarantee.
- When it WOULD be better: a system whose only text is our own (no customer input), where an
  estimate calibrated on that text is enough.

#### Option 0C: A pessimistic ratio for customer text only (e.g. 4 tokens a character) — rejected
- How it works: keep character caps; count customer-controlled characters at 4 tokens each.
- Pros: sound for customer text without changing the caps' units.
- Cons: a 24,000-character window counts as 96,000 tokens, larger than 0A's 24,000 for the same
  safety; replies and the summary, which cross between models, stay estimated.
- Why we didn't use it here: 0A gives the same guarantee with a smaller bound.
- When it WOULD be better: if the caps' units couldn't change (an external API contract in
  characters).

#### Option 0D: Real tokenizers, or ratios measured per model — rejected for now
- How it works: tokenize with each model's tokenizer (tiktoken, sentencepiece), or store a
  measured worst-case ratio per model.
- Pros: the tightest bound; small-context models could pass.
- Cons: several providers don't publish their tokenizers, the libraries aren't dependencies, and
  a measured ratio holds only for the text it was measured on.
- Why we didn't use it here: 0A's ceiling needs no tokenizer and can't be beaten by a clever
  string.
- When it WOULD be better: running a small local model whose context can't afford 0A's slack.

#### Option 0E: Count tokens at run time with the provider's count endpoint — rejected
- Cons: an extra request per call against free requests-per-day limits, and a guarantee that
  arrives at run time instead of at startup.
- When it WOULD be better: paid providers, as a last-line guard next to the static bound.

### Part 1 — How many read calls a turn may run

#### Option 1A: A read budget per turn; once it's spent, the read tools are withdrawn — CHOSEN
- How it works: `TURN_MAX_READ_CALLS` (default 5) caps the read calls a turn runs in total,
  counting duplicates. When a response's reads fit the remaining budget, they all run. When the
  budget is spent, later requests in the turn offer only the write tools, so the model replies,
  proposes or escalates, and no call or refusal is spent on asking again. A response whose reads
  exceed what's left (asking for 4 with 3 left, or calling a withdrawn tool anyway, which some
  models do) runs nothing and gets a note: "This turn can look up N more orders. Look up at most
  N, or ask the customer which orders they mean." That refusal is its own outcome
  (`read_budget_refused`), **not** malformed: a six-order question is ordinary traffic, and
  counting it as malformed would end the turn as `malformed_budget`, an escalation to a person.
  Repeated refusals are bounded by `TURN_MAX_MODEL_CALLS`.
- Pros: one number bounds tool results, database work and security-log writes for the turn;
  withdrawing the tools saves the call 1C wanted to save without its partial state; legitimate
  multi-order questions end in a reply, not an escalation; the all-or-nothing contract stays.
- Cons: a customer asking about six orders gets five looked up and is asked about the rest. The
  tool list changes mid-turn, so the recording key and the prompt fingerprint cover both lists.
- Cost/latency/complexity: a counter, a filtered tool list, one outcome and one note.

#### Option 1B: A per-response cap only — rejected
- Cons: the turn total is K × earlier steps, so the bound and the database work scale with two
  numbers; it doesn't express "lookups per customer message".
- When it WOULD be better: read tools of very different cost, where per-tool, per-response caps
  express the real limit.

#### Option 1C: Run the first K calls, refuse the rest — rejected
- Cons: breaks "nothing in a refused response runs", so which calls run depends on the order the
  model wrote them; mixed results and refusals for one request. 1A's withdrawn tools save the
  same call without it.
- When it WOULD be better: slow read tools whose order doesn't matter, where a wasted model call
  costs more than partial states.

#### Option 1D: `parallel_tool_calls: false` or `tool_choice: "none"` — rejected as the bound
- Cons: hints, not bounds: no provider's handling is captured, and one that ignores them puts us
  back at the unbounded case. `tool_choice: "none"` also forbids `escalate` and `propose_refund`.
  One-call-at-a-time turns a three-order question into three model calls.
- When it WOULD be better: as an extra hint on top of 1A, once captured per provider.

#### Option 1E: Refuse at run time when the next prompt would overflow — rejected
- Cons: a runtime failure the customer sees, after the lookups already ran; it bounds neither
  database work nor quota.
- When it WOULD be better: as 0E, a last-line guard next to the static bound.

### Part 2 — What the model sees after a malformed response

#### Option 2A: One fixed-size note; the malformed response isn't echoed — CHOSEN
- How it works: a malformed response with tool calls is answered as an empty reply already is:
  one system note, "Your last response was not run: <problem>. <what to do>", and the response
  itself is not sent back, so no tool-call ids need answering. Every model-controlled string in
  a problem (a tool name, an argument key) goes through `ascii()` and then is cut to
  `CORRECTION_ECHO_MAX_CHARS` (64) (S2).
- Captured 2026-11-19 (`system_note` fixtures): Groq, Gemini, Cloudflare and OpenRouter (ERNIE)
  all accept this shape (200). Three answered with the lookup; Cloudflare's gpt-oss-20b put the
  call in its reasoning channel and returned neither a call nor text, which the loop treats as
  an empty reply (k=1; pinned). Acceptance is settled; how well each model recovers is measured
  by ADR-026 cases with a scripted first step (S7).
- Pros: an exact bound: at most one note per step. It removes the quadratic echo and the free
  text the model planted in its own bad response.
- Cons: the model doesn't see its own malformed calls; whether it recovers as often is measured,
  not assumed (S7).
- Cost/latency/complexity: smaller prompts after a mistake; one rendering function; the
  fingerprint changes once.

#### Option 2B: Keep echoing, cut the quoted strings — rejected
- Cons: still one message per call, and calls per response are bounded only by the output cap
  (~500): ~20K tokens a correction. Fixes the quadratic part, leaves the linear part.
- When it WOULD be better: combined with 2C, if S7's measurements show models recover worse
  without the echo.

#### Option 2C: Echo at most K calls, answering each — rejected, the measured fallback
- Cons: we'd send an assistant message the model never wrote; dropped calls carrying provider
  data (Gemini's signatures) need care; more states than 2A.
- When it WOULD be better: if S7's scripted cases show recovery under 2A is lower than under
  today's echo (measured on the current code before 2A ships).

#### Option 2D: Keep the echo and count it in the bound — rejected
- Cons: quadratic in the output cap, so larger than every listed model; the check would have to
  be switched off.
- When it WOULD be better: never with an unbounded echo.

### Part 3 — The summary's input

#### Option 3A: The oldest messages in chunks of at most M; a chunk that keeps failing on its content is skipped with a marker — CHOSEN
- How it works: `SUMMARY_MAX_MESSAGES` (default 9, `SUMMARY_TRIGGER_MESSAGES + 1`) caps a job:
  `plan_summary` takes the *oldest* M uncovered messages beyond the window and covers through
  the newest of those; a backlog drains one chunk per turn with nothing skipped. A chunk whose
  job fails on its **content** (the provider refused the input, the context was too long, the
  summary output was empty or invalid) `SUMMARY_CHUNK_MAX_FAILURES` (3) times is covered by a
  row holding the previous summary plus a marker ("[N messages could not be summarized]"), and
  `summary_gap` is logged. **Provider** failures (unavailable, rate limit, spent quota, timeout)
  never count: the dependency being down isn't the chunk's fault (the project's rule from
  ADR-016). The count lives in its own table, `summary_chunk_failures` (`conversation_id`
  primary key, `chunk_through` event id, `content_failures`, `updated_at` from the clock): one
  row per conversation, upserted by the summary job, reset when the chunk it describes changes.
  It can't reach a prompt or the customer's event stream whatever their filters do. `llm_calls`
  alone isn't enough: an empty or tool-call summary is logged there as `ok` (the call
  succeeded; `write_summary` rejects the text afterwards). A `ContextTooLong` still counts toward
  the skip, so the summary recovers, but after this ADR it also means the bound is wrong (S5).
- Concurrency: two turns can plan the same chunk while the first job runs; the second stored row
  is dropped by `insert_summary`'s guard (a row covering this event or a later one already
  exists), so coverage stays contiguous. An in-process guard skips planning a job for a
  conversation whose job is still running, saving the wasted call; across processes the
  database guard still holds.
- Pros: the summary bound's "messages to add" becomes true; summaries recover after an outage on
  their own; a poisonous chunk can't block the summary forever; nothing is dropped silently.
- Cons: after a long outage the summary lags (a 100-message backlog needs 11 successful jobs);
  a skipped chunk's content is lost to the summary (logged, and it was in the window when it
  was current).
- Cost/latency/complexity: a slice, a count, a marker row, an in-process set.

#### Option 3B: A byte budget instead of a message count — rejected
- Cons: every message is already capped in bytes (Part 0), so a count is already a size bound;
  a budget adds the one-message-too-large case.
- When it WOULD be better: uncapped or very uneven messages (pasted documents).

#### Option 3C: Keep the newest uncovered messages, skip the oldest — rejected
- Cons: drops what the customer said, silently and for good.
- When it WOULD be better: chat whose old turns have no lasting value.

#### Option 3D: Summarize the whole backlog in one job, in stages — rejected
- Cons: several calls per job against free requests-per-day limits, right after an outage.
- When it WOULD be better: paid providers, very long conversations, a summary that must be
  current (a handover note).

#### Option 3E: Fail closed when the backlog is too big — rejected
- Cons: punishes the customer for our outage; memory is best-effort and never authorizes.
- When it WOULD be better: if the summary were load-bearing for safety. It isn't.

### Smaller decisions

- **S1. Settings and validation** (startup, like ADR-022 S2): `TURN_MAX_READ_CALLS` (5, ≥ 1);
  `SUMMARY_MAX_MESSAGES` (9, ≥ `SUMMARY_TRIGGER_MESSAGES + 1`); `SUMMARY_CHUNK_MAX_FAILURES` (3,
  ≥ 1); `CUSTOMER_MESSAGE_MAX_BYTES` (4,000) < `PROMPT_WINDOW_MAX_BYTES` (24,000);
  `LLM_MAX_OUTPUT_BYTES` (6,000). Code constants, part of the bound, not knobs:
  `CORRECTION_ECHO_MAX_CHARS` (64), `REQUEST_OVERHEAD_TOKENS` (256), `MESSAGE_OVERHEAD_TOKENS`
  (16).
- **S2. Quoting model text in a note:** `ascii(text)` first, then cut to 64 characters with
  "...": at most 67 ASCII bytes. Cutting before escaping isn't a bound: 64 tag characters escape
  to a 642-character `repr` (measured).
- **S3. The bound** (`context_fit.py`), all in tokens at the Part 0 rules: system prompt and
  digest (bytes); summary (`LLM_MAX_OUTPUT_BYTES`); window (`PROMPT_WINDOW_MAX_BYTES`); tool
  schemas (bytes); earlier responses (earlier steps × output-token cap); tool results
  (`TURN_MAX_READ_CALLS` × largest result's bytes); notes ((`TURN_MAX_MODEL_CALLS` − 1) × the
  longest note's bytes, rendered with each problem at its longest after S2, covering malformed,
  empty-reply and read-budget notes, at most one per step); overhead (request + per message ×
  the largest message count); output. Summary: prompt, previous summary
  (`LLM_MAX_OUTPUT_BYTES`), `SUMMARY_MAX_MESSAGES` × the larger of a customer message and a
  reply (bytes), overhead, output. The "not bounded" list goes.
- **S4. The window's message count is capped** at `MEMORY_WINDOW_MESSAGES + SUMMARY_MAX_MESSAGES`
  (21), newest first, besides its byte budget, and `window_events` loads at most that many
  newest messages plus the job's oldest M uncovered ones (≤ N + 2M rows). Messages the cap drops
  that no summary covers are logged as `summary_gap`, as the byte cut does today. This bounds
  per-message overhead and the query's rows during a summary outage.
- **S5. Telemetry; one new alert.** Log fields `read_budget_refused` (asked, left),
  `summary_backlog` (uncovered messages left after the chunk) and `summary_chunk_skipped`. The
  exception: after this ADR every prompt fits its model by construction, so a `ContextTooLong`
  means the startup guarantee is false. On the summary path it's logged at error level as
  `context_bound_violated` and alerts (today it's only `summary_failed`); on the turn path it
  already alerts (`CONTEXT_TOO_LONG_ALERT`) and gets the same log field. Neither hides the
  evidence: the alert names the purpose, provider, model and prompt size.
- **S6. The prompt fingerprint and recording keys change** (new notes, tool lists, units). That's
  intended: `verified_models.json` is empty, and recordings made before stop matching, by design.
- **S7. Measuring 2A (ADR-026 cases with a scripted first step):** a case's `script:` field
  fixes its first k model responses (e.g. a malformed one), injected through the recording
  wrapper; the real model answers from there, so recovery after a malformed response is measured
  on demand. The malformed-recovery cases run on today's echo first, then on 2A; 2C replaces 2A
  if 2A recovers less.
- **S8. A model joins a list only with a `byte_ceiling` fixture**, like its tool-call proof
  (ADR-025 S4), **and a context of at least the larger purpose bound plus output** (about 63K
  with the defaults; the startup check computes it). State on 2026-11-19 (`.env.example` lists
  no models, so nothing configured breaks):

  | Model | `byte_ceiling` fixture | Context (provider's model API, read 2026-11-19) | Listable |
  |---|---|---|---|
  | groq `openai/gpt-oss-120b` | yes | 131,072 | yes |
  | gemini `gemini-3.5-flash-lite` | yes | 1,048,576 | yes |
  | cloudflare `@cf/openai/gpt-oss-20b` | yes | 128,000 | yes |
  | openrouter `baidu/ernie-5.5-flash-preview:free` | yes | 1,024,000 | yes |
  | gemini `gemini-3.1-flash-lite` | owed | 1,048,576 | not until captured |
  | openrouter `cohere/north-mini-code:free`, `dots-studio/dots-3-note-preview:free`, `recursive/flint-v1.0:free` | owed | 256,000, 512,000, 262,144 | not until captured |
- **S9. Retired setting names are refused at startup.** `CUSTOMER_MESSAGE_MAX_CHARS`,
  `PROMPT_WINDOW_MAX_CHARS` and `LLM_MAX_OUTPUT_CHARS`, if set, stop the service with a message
  naming the replacement; otherwise an existing `.env` would be silently ignored and the default
  would apply. The same table takes ADR-025's replaced `LLM_PROVIDER`, `LLM_MODEL` and
  `LLM_API_KEY`, which today are silently ignored too.
- **S10. Stated assumptions in the bound** (everything else is a ceiling): the two overhead
  constants (`REQUEST_OVERHEAD_TOKENS`, `MESSAGE_OVERHEAD_TOKENS`), and that the model's own
  earlier responses re-read at no more than its output-token cap. The provider re-serializes
  tool-call arguments into its chat template, and re-tokenizing generated text needn't give the
  same count, so this is an assumption, not a proof. Evidence when wanted: an `echo_ceiling`
  capture that sends a maximum-length earlier response back and compares the provider's input
  count with the cap.

## Decision

0A, 1A, 2A and 3A, with S1–S10. Each turns an assumption in the S12 bound into an enforced limit
or a measured ceiling, so the startup check becomes a guarantee for every listed model with a
`byte_ceiling` fixture, and keeps ADR-022's contracts: nothing in a refused or malformed response
runs, and summaries are best-effort and never authorize. Fallbacks with their triggers: 2C if
S7 shows worse recovery; 0D for a small local model; 1D as an extra hint once captured.

### Tests (each with a negative control)
| Test | Negative control |
|---|---|
| Each `byte_ceiling` fixture's input tokens ≤ its request's UTF-8 bytes, and > its characters (built, 3 models) | A planted byte count below the tokens; a planted character count above them (both caught) |
| Each `system_note` fixture is a 200 the adapter parses (built, 4 providers); Cloudflare's reasoning-channel answer is pinned | A planted 400 (caught) |
| A model without a `byte_ceiling` fixture can't be listed | Removing the check lists it |
| A customer message over `CUSTOMER_MESSAGE_MAX_BYTES` is refused even when under the character count (4-byte characters) | Counting characters lets 4× the bytes through |
| The window keeps at most 21 messages and 24,000 bytes; `window_events` returns at most N + 2M rows | Unlimited rows during a simulated outage |
| Reads within the remaining budget all run; exactly the budget runs (5 in one response, or 2 + 3) | An off-by-one refuses the fifth |
| A response asking for more reads than are left runs nothing (no lookup row, no query), gets the budget note, and doesn't count as malformed | Counting it as malformed escalates a six-order question on `malformed_budget` |
| Once the budget is spent, the next request offers only the write tools | Offering read tools again lets the model ask a sixth time |
| A withdrawn read tool called anyway is refused, not run | Looking it up in `READ_TOOLS` unconditionally runs it |
| A malformed response of 51 calls with an 8,000-character unknown name gets one note within the bound's notes part | Today's echo (415,128 characters measured) |
| A note quoting 64 tag characters stays within 67 ASCII bytes | Cutting before escaping gives 642 characters |
| A malformed response isn't sent back; the next request holds the note, not the calls | Echoing puts the calls back |
| Property (Hypothesis): any script of responses within the output caps, through the real loop, never sends a prompt whose bytes plus overhead exceed the turn bound minus the output part | Removing the read budget, the window cap or S2 finds a counterexample |
| The summary job takes the oldest M uncovered messages and covers through the newest; the next starts right after | Taking the newest M leaves a gap in the coverage chain |
| A backlog of 3M drains in 3 successful jobs, every message covered once | Covering through the backlog's newest skips the middle |
| A chunk that fails on its content 3 times is covered with the marker and logged; provider failures never count | Counting a quota error skips content during an outage |
| Two turns planning the same chunk store one row; in-process, the second job isn't started | Without the database guard, a stale job overwrites coverage |
| Raising `TURN_MAX_READ_CALLS` by one raises the turn bound by one largest result; raising `SUMMARY_MAX_MESSAGES` by one raises the summary bound by one largest message | A bound fixed at today's counts doesn't move |
| Settings below their minimums → startup refused | Removing the checks starts |
| A retired setting name set (`PROMPT_WINDOW_MAX_CHARS`, `LLM_PROVIDER`, …) → startup refused, naming the replacement | Ignoring it starts with the default |
| A model whose context is below the larger bound plus output can't be listed | Removing the comparison lists it |
| A content failure increments `summary_chunk_failures` for that chunk; a new chunk resets it; provider failures leave it alone | Keying the count by conversation only carries failures across chunks |
| A summary `ContextTooLong` logs `context_bound_violated` and alerts, and still counts toward the skip | Treating it as an ordinary content failure skips the chunk silently |

## Consequences

- The bound roughly triples (turn ≈ 44K, summary ≈ 62K tokens, exact figures from code); every
  listable model has at least 128K (S8), small local models don't (0D is their path).
- Worst-case prompts are far above Groq's 8K tokens a minute, and a long window can pass it even
  today. Groq answers such a request with "request too large", which no fixture pins: it would
  fall to the default (`InvalidRequest`, a page) instead of a reply that moves the next turn to
  another provider. Waiting can't help a request that never fits. The body gets captured and
  classified under ADR-023's rules (a separate decision, since moving a turn's first call to
  the next provider touches ADR-023's one-provider-per-turn rule); recorded in KNOWN_TRADEOFFS.
- CJK and emoji customers get fewer characters per message and in the window (bytes, not
  characters); English is unchanged.
- A customer asking about more than five orders in one message gets five and a question.
- A malformed response no longer reappears; recovery is measured (S7), with 2C as the fallback.
- After a summary outage the summary lags; a chunk that can't be summarized is skipped with a
  marker after three content failures.
- What would make us revisit: a second read tool with a very different cost (1B), a small local
  model in production (0D), or paid providers (0E, 3D).

## Interview-ready summary

"Our startup check promised every model's context could hold the largest possible prompt, but
it counted tokens as characters divided by three. I captured the providers' own counts for text
a customer could send: about ten times the estimate. So I replaced the estimate with a ceiling,
one token per byte, which byte-level tokenizers can't exceed, checked per model by a captured
fixture, and moved the caps on customer text to bytes. Then the unenforced assumptions: read
calls got a per-turn budget that withdraws the read tools when it's spent, because a customer
with six orders isn't a malformed response; the correction echo, which turned a 9.5K-character
response into a 415K-character prompt, became one fixed-size note; and the summary eats its
backlog in chunks, skipping one only after repeated content failures, never because the
provider was down. Asking the provider for one call at a time would've been easier, but it's a
hint, not a bound."

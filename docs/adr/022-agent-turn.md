# ADR-022: The Agent Turn — Steps, Budgets, Where the Conversation Lives, and Fencing Untrusted Text

**Status:** Accepted (2026-11-14). Revised 2026-11-14 after two outside reviews. Review 1: a
proposal on an order this run hasn't looked up is bounced back to the model before policy (S1);
the summary runs after the lease is released (S6); message and window size limits (S9); an alert
on repeated provider failures (Part 2); what the call budget counts (S2); text the model repeats
can persist (Part 1); no retention rule exists yet for conversation data (S10). Review 2: the
summary runs on a message threshold with its own deadline, and messages not yet summarized stay
in the window (S6); the smaller decisions numbered in order.

**Locked 2026-11-14.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Precedence:** fills in ADR-001's `AGENT_STEP` loop, ADR-002's storage, ADR-008's turn lease
and ADR-009's proposal path for the code that runs one customer message. The provider adapter
(AsyncOpenAI, retry, telemetry) is ADR-023; streaming to the client goes with the chat API
(ADR-003); the KB-search tool gets its own ADR. This ADR states only what the turn needs from
them.

## Context
Settled already:
- **ADR-001/006:** a state machine around a bounded tool loop. Read tools run in the loop; a
  write tool (refund, escalate) only proposes. At most one write call per model response, never
  mixed with other calls; a malformed response gets an error and a bounded retry, then the run
  escalates. No model call once a proposal exists.
- **ADR-008:** a turn holds a lease on the conversation (`active_turn_id`, `turn_expires_at`),
  heartbeats it, and fences every write on it; the turn-start transaction saves the customer's
  message; a lost turn's partial work is gone. `AGENT_STEP → COMPLETED` on a reply with no
  proposal, `→ ESCALATED` when the step or malformed-retry budget runs out.
- **ADR-009:** C01 runs on every customer message, never on tool output. A message while a
  proposal is pending gets a status template and never reaches the model. A new turn resumes an
  unfinished `AGENT_STEP` run and reuses its `run_order_lookups`, not its draft text or tool
  results. Proposal inputs are gathered before the customer lock; a `55P03` there is retried,
  then "please try again", and the run stays in `AGENT_STEP`.
- **ADR-002:** a window of recent turns, a rolling summary, a code-built past-ticket digest;
  N, K and the summary trigger are configuration.
- **ADR-020:** the tool contract (`ToolArgs`, `ToolContext`, `LostTurn`, canonical JSON). Its
  transactions set no timeouts; the turn must (KNOWN_TRADEOFFS).

Not settled:
1. ★ **Where the conversation lives:** there is no messages table, and nothing stores the
   summary or tool transcripts.
2. ★ **What a provider failure or a passed turn deadline does to the run.**
3. ★ **How untrusted text is fenced in the prompt.**
4. The turn's steps in order, its budgets and timeouts, and their numbers.

## Options Considered

### Part 1 — Where the conversation lives

#### Option A: The `events` table is the conversation log; tool transcripts are trace data, not memory — CHOSEN
- How it works: the turn-start transaction appends a `customer_message` event; a model reply is
  a `reply` event carrying its text (templated replies already are `reply` events). The window
  is at least the last N `customer_message`/`reply` events of the conversation, across runs,
  plus any older ones the newest summary doesn't cover yet (S6), within a size budget (S9). Tool
  calls and results live only inside one turn's prompt; they're emitted as per-step trace
  records (structured logs now, the observability ADR's tables later), never replayed into a
  later prompt. The rolling summary is an append-only `conversation_summaries` table
  (conversation, text, the last event id it covers, model, time); the row covering the latest
  event is used.
- Pros: one ordered log already feeds the resumable event stream (ADR-003), so what the
  customer saw and what the model sees next come from the same rows. Old tool results can't
  re-enter a prompt, which matches R02's fresh lookup per run and ADR-009's "tool results aren't
  reused". The summary history is auditable (ADR-002: "stored and traced").
- Cons: the model doesn't remember last turn's order facts and must look them up again (already
  accepted: KNOWN_TRADEOFFS, R02); its own earlier reply can still tempt it to propose without
  looking (S1 catches that before policy). **Stated precisely: tool output can't persist into a
  later prompt; text the model repeated in a reply can,** as an assistant message, for the next
  N messages (and longer through the summary). That's the same laundering channel as the
  summary (ADR-002), shorter-lived, and it's measured (EVAL_PLAN category 3).
- Cost/latency/complexity profile: one new table, two event kinds, one indexed window query.

#### Option B: A `messages` table holding the provider message list verbatim — rejected
- How it works: every system, user, assistant, tool-call and tool-result message is a row;
  the next prompt replays them.
- Pros: the next turn sees exactly what the model saw; trivial replay for debugging.
- Cons: duplicates the events the stream needs; tool results from older runs (stale order
  facts, KB text) re-enter later prompts, so an injection persists and tokens grow with every
  lookup; ties storage to one provider's message shape.
- Why we didn't use it here: replaying tool output is the persistence channel ADR-002 rejected
  memory frameworks for.
- When it WOULD be the better choice: an assistant whose tools are expensive and read-only and
  whose results stay valid, where re-calling them costs more than the replay risk.

#### Option C: The run's transcript as a JSONB column on `runs` — rejected
- How it works: the whole provider message list for the run is saved after each step, so a
  resumed run continues mid-loop.
- Pros: crash-resumable mid-turn.
- Cons: a growing row rewritten on every step under the lease; a resumed turn would trust
  in-flight tool results ADR-009 says not to; still needs another store for the cross-run
  window.
- Why we didn't use it here: resuming mid-loop is exactly what ADR-008/009 decided against.
- When it WOULD be the better choice: long autonomous tasks where a step is expensive and the
  intermediate results are trusted.

#### Option D: An external session store (Redis, a memory service) — rejected
- Why we didn't use it here: a second store without transactions shared with the lease and the
  run; ADR-005 keeps one database per service.
- When it WOULD be the better choice: very high message volume where the window must be served
  without touching Postgres.

### Part 2 — When the provider fails or the turn runs out of time

#### Option A: A templated "try again" reply; the run stays in `AGENT_STEP` — CHOSEN
- How it works: after ADR-023's retries inside the turn's deadline, a provider error (rate
  limit, timeout, 5xx, quota) or a passed deadline ends the turn with a templated reply
  (`try_again`: "I couldn't finish that just now. Please try again in a moment."; the same
  template as ADR-009's proposal-time lock timeout, with the cause, `provider` or `lock`, in the
  event payload), written fenced on the lease, and releases
  the lease. The customer's message is already saved and in the window, so the next turn simply
  answers it; nothing asks them to resend. The run stays in `AGENT_STEP`; the next message
  resumes it (ADR-009). After `TURN_PROVIDER_FAILURE_ALERT_AFTER` consecutive `try_again`
  replies with cause `provider` in one conversation (counted from its trailing reply events, no
  new column), the turn raises a rate-limited `conversation_provider_failures` alert, so "this
  customer can't get an answer" is visible before anything escalates on its own. A step-budget
  or malformed-retry exhaustion still escalates (ADR-008): those are the run's problem, not the
  provider's.
- Pros: an outage doesn't push every open conversation to a human (the same lesson as ADR-018's
  breakers); the same shape as ADR-009's proposal-time lock timeout; nothing half-done is kept.
- Cons: the customer has to try again; a provider that fails for one conversation forever never
  reaches a human on its own (made visible by the alert; an escalation counter is under
  Revisit). No money waits on it: a run in `AGENT_STEP` has no proposal.
- Cost/latency/complexity profile: one template, no new transition.

#### Option B: Escalate the run — rejected
- Pros: a human always picks it up.
- Cons: a provider outage escalates every active conversation at once and floods review, the
  failure mode ADR-018 removed from the refund path.
- Why we didn't use it here: an outage isn't a reason a person must handle this customer.
- When it WOULD be the better choice: a high-value channel with staff on hand, where any delay
  is worse than a handoff.

#### Option C: Finish the turn in the background (a worker retries the model later) — rejected
- Pros: the customer doesn't resend.
- Cons: `AGENT_STEP` isn't worker-claimed (ADR-008), so this needs a new claim path and a reply
  to a client that may have left; the customer may have sent more meanwhile.
- When it WOULD be the better choice: an asynchronous channel such as email, where replies are
  expected later anyway.

#### Option D: Keep trying past the deadline while the lease holds — rejected
- Why we didn't use it here: the lease would be renewed indefinitely and the conversation would
  stay locked (409s) while the customer waits on a dead provider.

### Part 3 — Fencing untrusted text in the prompt

#### Option A: Tool output in the provider's tool role as canonical JSON; free text inside a per-turn nonce fence — CHOSEN
- How it works: tool results are sent only as tool-result messages (never as user or system
  text), as the tool's canonical JSON. Any free-text field (KB article bodies, later order
  notes) is wrapped as `<untrusted-{nonce}>…</untrusted-{nonce}>`, where the nonce is random per
  turn and any occurrence of the closing marker inside the text is escaped first. The past-ticket
  digest and the rolling summary go in the same fenced form (ADR-002: both untrusted). The
  system prompt says fenced text is data. Customer messages are user messages, never fenced as
  instructions to follow.
- Pros: uses the provider's own role separation; a planted closing tag can't break out because
  it can't guess the nonce; structural, so ScriptedLLM tests assert it (ADR-006 rule 3).
- Cons: a mitigation, not a guarantee: the safety claim (ADR-001) rests on the write gate, and
  the fence only lowers the injection success rate, which is measured (EVAL_PLAN categories).
- Cost/latency/complexity profile: a few tokens per fenced block.

#### Option B: A fixed delimiter (`<untrusted>…</untrusted>`) — rejected
- Why we didn't use it here: an article containing `</untrusted>` closes the block.
- When it WOULD be the better choice: content that can't contain markup (numbers, enums).

#### Option C: A quarantined model reads untrusted text and returns only structured fields (dual-LLM pattern) — rejected
- Pros: the privileged model never sees raw untrusted text.
- Cons: an extra model call per tool result; KB answers need the text itself; the write gate
  already removes the high-impact path.
- When it WOULD be the better choice: an agent whose write tools can't be gated by code.

#### Option D: No fencing; rely on the system prompt — rejected
- Why we didn't use it here: it's the baseline loop's behavior (EVAL_PLAN), kept only for the
  comparison.

### Smaller decisions
- **S1. The turn, in order.**
  1. *Start* (one transaction, lock order `customers → conversations → runs`, ADR-009): take the
     lease (0 rows → 409 "a reply is in progress"); append `customer_message`; run C01 on it;
     find the conversation's unfinished run. C01 may run inside this transaction only because v1
     is deterministic pattern matching; a model classifier (allowed by ADR-009's asymmetry
     principle) is a network call and must run before it, never while the customer lock is held.
     - A pending proposal (`AWAIT_APPROVAL` … `NEEDS_RECONCILIATION`): status template, C01
       handled as ADR-009 says, lease released, **no model call**.
     - No unfinished run: `INTAKE` creates one in `AGENT_STEP`. An `AGENT_STEP` run: resume it.
     - C01 hit in `AGENT_STEP`: `→ ESCALATED`, `needs_person` template, no model call.
  2. *Prompt:* system prompt, fenced digest and summary, the window, the new message. The digest
     is built by code from the database at every turn start (deterministic, so a resumed run
     rebuilds it rather than storing it).
  3. *Loop*, no database connection held across a model call (ADR-005), heartbeat running:
     - no tool calls → the text is the reply: `→ COMPLETED`, `reply` event and the lease release
       in one transaction;
     - read calls only → run each in order with a `ToolContext`, append results, loop;
     - one valid write call alone → validate (Pydantic); a `propose_refund` whose `order_id`
       isn't in this run's `run_order_lookups` gets an error tool result ("look up the order in
       this conversation before proposing a refund") and counts as malformed, so a model
       proposing from its own earlier reply looks the order up instead of escalating on R02.
       This doesn't weaken R02: policy still enforces it on whatever gets past. Otherwise assign
       `action_id`, ADR-009's proposal path, its template reply, lease released; the model is
       never called again. The system prompt says to look an order up in the current
       conversation before proposing a refund on it;
     - malformed (bad JSON, unknown tool, failed validation, a write mixed with other calls) →
       an error tool result naming what was wrong, nothing in that response runs; past the
       malformed budget → `ESCALATED`;
     - step budget reached → `ESCALATED`;
     - provider failure or deadline → Part 2;
     - `LostTurn` (a tool or the heartbeat) → stop; write nothing more.
  4. Text that comes with tool calls in one response is interim: not a reply, not saved
     (the streaming ADR may show it as status).
- **S2. Budgets, timeouts and their defaults**, all from the environment and validated at
  startup like `WorkerTimings`: `TURN_MAX_MODEL_CALLS` (6), `TURN_MAX_MALFORMED` (2),
  `TURN_DEADLINE_SECONDS` (60, monotonic from the turn start), `TURN_LEASE_SECONDS` (30),
  `TURN_HEARTBEAT_SECONDS` (10), `TURN_STATEMENT_TIMEOUT_SECONDS` (5),
  `TURN_LOCK_TIMEOUT_SECONDS` (2), `MEMORY_WINDOW_MESSAGES` (12), `DIGEST_TICKETS` (5),
  `CUSTOMER_MESSAGE_MAX_CHARS` (4000), `PROMPT_WINDOW_MAX_CHARS` (24000),
  `TURN_PROVIDER_FAILURE_ALERT_AFTER` (3), `SUMMARY_TRIGGER_MESSAGES` (8, about four turns),
  `SUMMARY_DEADLINE_SECONDS` (30). Rules: heartbeat ≤ lease / 3; lock timeout ≤
  statement timeout < deadline; the message cap < the window budget. The defaults are starting
  values to tune from measured telemetry, not measurements.
  **What the budgets count:** `TURN_MAX_MODEL_CALLS` counts every provider call the turn makes,
  whatever comes back (calls that only led to read tools, malformed responses, and the calls
  answering an error result); retries inside one adapter call (ADR-023) are one call.
  `TURN_MAX_MALFORMED` counts malformed responses, including a proposal bounced by the lookup
  check in S1.
- **S3. Every turn transaction sets `statement_timeout` and `lock_timeout`** with `SET LOCAL`,
  through the helper the worker uses, including the ones a tool opens (the `ToolContext`
  connection is handed over with them set). This closes the gap ADR-020's build recorded.
- **S4. A turn holds no database connection while waiting on the model**; each tool call and
  each write checks one out. A crashed turn blocks its conversation for at most one lease.
- **S5. Every turn write is fenced** on `active_turn_id = :t` (ADR-008), with the row lock the
  order-lookup fence uses (`FOR SHARE` on the conversation) where a write follows a check.
- **S6. The rolling summary runs outside the turn, on a threshold.** The reply and the lease
  release commit together. The window is every message event after the newest summary's last
  covered event, and at least the last N, so a message that has left the last N but isn't
  summarized yet stays in the prompt instead of falling into a gap. When the number of those
  unsummarized messages beyond the last N reaches `SUMMARY_TRIGGER_MESSAGES` (ADR-002's "summary
  trigger"), the turn starts the summary as a background task holding no lease: a quick
  follow-up message is never a 409, and a long conversation costs one summary call per
  threshold's worth of messages, not one per turn. The task has its own deadline,
  `SUMMARY_DEADLINE_SECONDS`, because the turn's no longer bounds it. It reads events up to a
  fixed last event id (events are append-only) and inserts only if no row covers a later event;
  readers use the row covering the latest event, so a concurrent or late summary can't replace a
  newer one (two overlapping turns may both summarize: a wasted call, never a wrong summary). It's
  best-effort: a failure, a timeout or a process that dies first leaves the old summary, the
  unsummarized messages stay in the window, and the next turn at or over the threshold tries
  again. Only if they then exceed the window budget (S9) are the oldest dropped: that's the
  summary gap, logged (`summary_gap`) and counted.
- **S7. The write tools' schemas:** `propose_refund(order_id, amount_minor, currency)` and
  `escalate(reason_code)` with a closed set of codes; neither has an identity field.
  `amount_minor` is the unit the lookup returns as `charged_minor` (ADR-020).
- **S8. What the turn needs from the provider (ADR-023):** one call taking messages, tool
  schemas and a deadline, returning a normalized response (text, tool calls with raw
  arguments, finish reason, token counts) or one of a closed set of errors (rate limit,
  timeout, provider error). It never retries past the deadline.
- **S9. Size limits.** A customer message over `CUSTOMER_MESSAGE_MAX_CHARS` is refused before
  the lease is taken, so nothing is saved (the chat API maps it to 413); one huge message can't
  make the next N turns expensive (denial-of-wallet) or overflow the context. The prompt builder
  keeps the window within `PROMPT_WINDOW_MAX_CHARS`, dropping the oldest messages first and
  logging `window_trimmed`. Characters stand in for tokens until ADR-023's adapter knows the
  model's tokenizer; model output is bounded there by its maximum output tokens.
- **S10. Retention.** No retention rule exists yet for any conversation data. This adds two more
  kinds of it (reply text, summaries) beside the customer messages; the retention decision,
  when it's made, covers all three together (KNOWN_TRADEOFFS).

## Decision
Part 1 A, Part 2 A, Part 3 A, S1–S10.

### Tests (each with a negative control)
| Test | Negative control |
|---|---|
| Two messages at once: one turn, one 409; the message and the lease are written in one transaction | Taking the lease without its condition starts two turns |
| A message while a proposal is pending gets the status template and **zero** model calls (strict ScriptedLLM, empty script) | Routing it to the model makes an unexpected call |
| C01 on a customer message in `AGENT_STEP` → `ESCALATED`, no model call; the same words in a KB result → no escalation | Running C01 on tool output escalates on the KB article |
| A text reply → `COMPLETED`, one `reply` event with the text, lease released | Not releasing the lease makes the next message a 409 |
| Read calls run in order; results reach the model only as tool-result messages | Sending a result as user text fails the structural assertion |
| A KB body containing `</untrusted-…>` and fake instructions stays inside one fence | A fixed delimiter lets the planted closing tag end the block |
| One valid `propose_refund` → ADR-009's transition and template; the model is never called again | Feeding the outcome back to the model makes an unexpected call |
| "Yes, refund it" after a reply naming the order: a proposal without a lookup in this run gets the error result; the model then looks it up and the proposal succeeds | Passing it straight to policy → `ESCALATED` on R02 |
| Each scripted bad output (ADR-006 library) gets an error result and nothing in it runs; the malformed budget + 1 → `ESCALATED` | An unbounded retry exhausts the script instead |
| The step budget: exactly `TURN_MAX_MODEL_CALLS` calls, counting calls that answered a malformed response, then `ESCALATED` | An off-by-one makes one call too many |
| A provider error after retries, and a passed deadline: the `try_again` template, run still `AGENT_STEP`, lease released, no escalation | Escalating on provider failure fails the status assertion |
| `TURN_PROVIDER_FAILURE_ALERT_AFTER` consecutive provider `try_again` turns → one alert; an answered turn in between resets the count, and a lock-timeout `try_again` doesn't count | Counting all `try_again` replies, not the trailing provider ones, alerts after the reset |
| A message sent right after a reply, while its summary is still being written, is accepted | Writing the summary under the lease makes it a 409 |
| A summary inserted late, covering an older event, is never used over a newer one | Reading the newest row by insert order uses the stale summary |
| A message over the cap is refused and nothing is saved; a window over its budget drops the oldest messages first and logs it | Without the trim the prompt exceeds the budget |
| A takeover mid-turn (lease expired, another turn started): no reply, no transition from the stale turn | An unfenced final write lands the stale reply |
| A resumed run reuses its lookups; no earlier tool result appears in the prompt | Replaying an older tool result fails the prompt assertion |
| The window is at least the last N message events across runs, plus any not yet summarized; the summary is fenced; a failed summary keeps the old one and its messages stay in the window | Dropping the fence on the summary fails the structural assertion |
| Four turns past the last N with `SUMMARY_TRIGGER_MESSAGES` = 8 make one summary call, not four | Summarizing on every turn past the last N makes four |
| The summary call carries its own `SUMMARY_DEADLINE_SECONDS` deadline; a timeout leaves the old summary | Calling it with no deadline fails the recorded-deadline assertion |
| Only when unsummarized messages exceed the window budget are the oldest dropped, with `summary_gap` logged | Dropping messages beyond the last N without a summary loses them silently |
| Every turn transaction, including a tool's, runs with the configured timeouts (read back with `current_setting` inside the transaction) | Skipping the helper for the tool connection reads the server default |
| No tool schema offered to the model has an identity field | Adding `user_id` to `propose_refund` fails it |
| Configuration: a heartbeat above lease / 3, or a statement timeout at or above the deadline, is refused at startup | Removing the check starts with it |

## Consequences
- New: a turn module in the agent (the loop, its prompt builder, the write-tool schemas),
  `TurnTimings` from the environment, a `conversation_summaries` table and migration, event
  kinds `customer_message` and `reply` with text, `.env.example` entries.
- The chat API (ADR-003) calls one function per customer message and maps its outcome to HTTP
  (409, accepted); streaming is layered on in its own ADR without changing the turn's steps.
- `KNOWN_TRADEOFFS.md`: the model re-looks-up orders in every run; text the model repeated in a
  reply persists for N messages; the summary gap on repeated summary failures; the
  provider-failure template means a customer tries again; the fence is a mitigation measured by
  evals, not a guarantee; characters stand in for tokens; no retention rule for conversation
  data; the default numbers are unmeasured.
- `EVAL_PLAN.md` category 3 gains a case: injected KB text the model repeats in a reply, checked
  over the following turns.
- **Revisit if:** evals show customers repeating context the window dropped (raise N or add
  ADR-002's retrieval tool); provider-failure alerts show conversations stuck in `AGENT_STEP`
  (add a failure count that escalates); C01 gains a model classifier (move it out of the start
  transaction, S1); the streaming ADR needs interim text shown.

## Interview-ready summary
"A turn is one customer message. It takes a lease on the conversation, saves the message and
runs the legal-or-abuse check before the model ever sees it; if a refund is already pending, the
model isn't called at all. Then a bounded loop: read tools run in order, a single write call
leaves the loop as a proposal that code evaluates, and anything malformed gets an error and a
bounded retry before a human takes over. If the model tries to refund an order it only remembers
from its own earlier reply, the turn sends it back to look the order up, so a shortcut doesn't
become a hard policy violation and a 'yes, refund it' doesn't escalate. The conversation lives
in the same event log the customer's stream reads, and tool results are never replayed into a
later prompt; what can persist is text the model itself repeated, which I measure rather than
claim away. If the provider fails, the customer gets 'please try again' and the run waits,
rather than escalating every conversation during an outage. Untrusted text goes in a fence with
a random per-turn nonce, so a planted closing tag can't break out, but I don't count that as the
safety guarantee: the write gate is."

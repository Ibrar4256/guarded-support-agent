# ADR-020: The Order-Lookup Tool — Its Contract, and Logging Cross-User Attempts Without an Oracle

**Status:** Accepted (2026-11-13). Revised 2026-11-14 after one outside review: the probe and its
log are best-effort and can't change the answer (Part 2); a database error on the answering query
gets one generic tool error (S6); the R02 insert is fenced by the turn lease (S7); the model also
gets a display amount, and the delivery date is in UTC (Part 3); rate limiting and unguessable IDs
recorded under Revisit.

**Locked 2026-11-14.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Precedence:** fills in ADR-001 ("read tools execute inside the loop"), PROJECT_BRIEF design
requirement 2 and ADR-009's R02 lookup log for the first tool. It is the first tool, so it also
sets the minimal tool contract the LLM loop (ADR-001, ADR-006) will consume; the loop's own
decisions (provider schemas, malformed calls, several calls per turn) stay with ADR-006.

## Context
Settled already:
- **PROJECT_BRIEF requirement 2:** identity comes from the session, never from model-filled
  arguments; the lookup checks ownership in **one query** (`WHERE id = :order AND user_id =
  :session_user`) and returns the **same "not found"** for a missing order and another customer's:
  same message, same shape, same code path. The cross-user case goes to a server-side security
  log, never to the model.
- **EVAL_PLAN category 4:** a deterministic test compares the two responses byte for byte, with a
  control that a planted "belongs to another customer" message fails it.
- **ADR-009 R02:** every order the run's lookup returns is persisted in `run_order_lookups`; the
  proposal and the pre-send re-check read it.
- **ADR-001:** read tools run inside the loop; their output is untrusted data to the model.

Not settled:
1. **The tool contract**, before the loop exists: what the tool receives from code and from the
   model, and what it returns.
2. ★ **How to know a not-found was a cross-user attempt** (for the security log) without that
   knowledge leaking into the response, its shape, its code path or its queries.
3. **What the model sees** about a found order.

## Options Considered

### Part 1 — The tool contract

#### Option A: A minimal tool protocol now, in `agent/tools/` — CHOSEN
- How it works: a tool is a typed **arguments model** (only what the model fills in; strict, no
  extra fields, and no identity field at all) plus an async function that gets a **context built
  by code** (`session_user`, `conversation_id`, `turn_id`, `run_id`, a database connection) and
  returns a **model-facing result**: one canonical JSON string (sorted keys, compact
  separators), so byte-for-byte means what it says. The loop later derives each provider's tool
  schema from the arguments model.
- Pros: the thing the requirement is about (the exact text the model receives) is the tool's own
  output and testable now; identity can't be in the schema because the arguments model is the
  schema; R02's log is written where the order is returned.
- Cons: a contract designed one tool ahead of the loop may need a second look when the loop
  arrives (recorded under Revisit).
- Cost/latency/complexity profile: a dataclass, a base model and one function.

#### Option B: A plain database function now; the tool contract with the loop — rejected
- How it works: `find_order(conn, order_id, user)` returning a row or `None`; the loop wraps it.
- Pros: nothing designed ahead of its consumer.
- Cons: the model-facing text, the thing that must be uniform, would be produced by code that
  doesn't exist yet; the byte-for-byte test would test a row, not a response.
- Why we didn't use it here: it postpones the requirement's actual subject.
- When it WOULD be the better choice: a tool whose output is passed through unchanged.

#### Option C: A framework's tool decorator — rejected
- Why we didn't use it here: ADR-001 hand-rolls the loop; a framework's tool types would leak in.
- When it WOULD be the better choice: a project built on that framework's loop.

### Part 2 — Telling a cross-user attempt from a missing order, for the security log only

#### Option A: The answering query as written; a separate existence probe that feeds only the log — CHOSEN
- How it works: the answer comes from the brief's one query (id **and** session user). If it
  returns nothing, the **response is built first**, from a constant, and then a probe
  (`SELECT EXISTS (… WHERE id = :order)`) decides only whether a security log line is written. The
  probe runs for **every** not-found, missing and foreign alike, so both take the same queries and
  the same path; only the log differs, and the log is server-side.
  - **The probe and its log are best-effort and isolated.** They run in their own short
    transaction after the answering one has finished, inside a catch-all: a probe or log failure
    (a lock timeout, a dropped connection) is itself logged and the constant not-found is returned
    unchanged. Otherwise a probe error would turn a miss into a tool error, only for misses: a new
    difference between the cases. (A not-found writes nothing, so there is no R02 insert for a
    probe failure to roll back either way.)
- Pros: no foreign data is ever read into the answering path, so a bug in response building can't
  leak it; the two responses are identical by construction and by test; monitoring still sees
  enumeration attempts.
- Cons: one extra query on every not-found; the log write itself is a small timing difference
  between the two cases (a few hundred microseconds, server-side, behind a model round trip of
  seconds). Handing the log line to an in-process queue would make both paths do exactly the same
  work before returning; we chose not to, since the difference is far below the round trip's
  noise. The log detects enumeration, it doesn't prevent it (see Revisit).
- Cost/latency/complexity profile: one indexed existence check per not-found.

#### Option B: One query by id, returning an ownership flag — rejected
- How it works: `SELECT …, owner_user_id = :session AS mine FROM orders WHERE id = :order`; the
  code branches on the flag.
- Pros: one query in both cases; the security log needs no probe.
- Cons: another customer's row is read into the answering path, so the uniform response depends on
  every branch being written correctly; it isn't the brief's query.
- Why we didn't use it here: ownership belongs in the `WHERE`, so a bug can't return a foreign row.
- When it WOULD be the better choice: never for a tool whose output goes to a model.

#### Option C: Log every not-found, no probe — rejected
- How it works: each not-found writes a log line with the order id; monitoring joins it later.
- Pros: no extra query; trivially the same path.
- Cons: typos and genuine mistakes drown the signal; a later join with `orders` is the probe again,
  just outside the service.
- Why we didn't use it here: the requirement is to see cross-user attempts, not all misses.
- When it WOULD be the better choice: a high-volume lookup where one extra query per miss matters.

#### Option D: No security log — rejected
- Why we didn't use it here: the brief requires it; enumeration attempts should be visible.

### Part 3 — What the model sees for a found order

#### Option A: The order's own facts, nothing derived — CHOSEN
- How it works: `order_id`, `status`, `charged_minor` (what a proposal must use), `currency`,
  `charged_display` (for example `"50.00 USD"`, formatted in the core from the currency's ISO 4217
  minor-unit exponent), and the delivery date when delivered: a date **in UTC** (R08 uses the full
  timestamp, so the date is for conversation only; it is UTC everywhere so nobody "fixes" it to
  local time inconsistently). No refund history, no "refundable" amount, no policy numbers. Models
  regularly read `charged_minor: 5000` as $5,000; the display string only changes what the model
  says, never what a proposal carries.
- Pros: the model can talk about the customer's order; policy stays in code (ADR-001), and the
  model can't anchor a proposal on a figure the policy computes differently later.
- Cons: the model can't tell the customer how much is left to refund; that answer, if wanted,
  comes from the policy check's reply, not the lookup.
- Cost/latency/complexity profile: none.

#### Option B: Facts plus derived refund state (refunded so far, refundable now) — rejected
- Pros: better answers to "how much can I get back?".
- Cons: the lookup would compute half of ADR-009's capacity rules outside the policy core, and the
  number can be stale by the time a refund is proposed.
- Why we didn't use it here: it duplicates policy in a read tool.
- When it WOULD be the better choice: a read-only status assistant with no refund action.

#### Option C: The whole row — rejected
- Why we didn't use it here: `owner_user_id`, `chargeback_open` and timestamps the model doesn't
  need are surface for leaks and injection-shaped content later (order notes, when they exist).

### Smaller decisions
- **S1. Arguments:** `order_id` only. A string that doesn't match the order-id pattern gets the
  **same** not-found response (the format is public; one response is simpler and leaks nothing). A
  missing or non-string `order_id` is a malformed tool call, handled by the loop (ADR-006).
- **S2. R02 log:** a found order is recorded in `run_order_lookups (run_id, order_id)` in the same
  transaction as the read, `ON CONFLICT DO NOTHING`; a not-found records nothing.
- **S3. Canonical output:** results serialize with sorted keys and compact separators; the
  not-found result is a module constant, so there is exactly one not-found text.
- **S4. The security log:** a dedicated logger (`agent.security`), one structured `WARNING` line
  `cross_user_order_lookup` with the session user, the run and the order id. It is not an alert:
  monitoring decides what is worth paging on.
- **S5. As the runtime role:** reads `orders`, inserts into `run_order_lookups`; the tests run as
  that role, so a missing grant fails them.
- **S6. A database error on the answering query** returns one generic tool error ("order lookup
  is temporarily unavailable"), identical whatever the input: never "not found", which would be a
  lie the model passes on to the customer. It can't leak ownership, because it fails before
  ownership is known. The same error covers a currency missing from the exponent table (a data
  error: the display is never guessed).
- **S7. The R02 insert is fenced by the turn lease** (ADR-008: every write a turn makes is
  conditional on `active_turn_id = :t`): it inserts only while this turn still holds the run's
  conversation, and 0 rows means the turn was lost, which the tool raises to the loop like a lost
  claim. A stale turn can't record a lookup into a run another turn now owns. R02 is provenance,
  not ownership (R03 checks that), so this isn't a money issue, but it's the stated rule.

## Decision
Part 1 A, Part 2 A, Part 3 A, S1–S5.

### Tests (each with a negative control)
| Test | Negative control |
|---|---|
| A missing order and another customer's order get byte-for-byte identical results | A planted "belongs to another customer" message fails it |
| Both not-found cases run the same queries (statement texts recorded on the connection) | A probe run only for foreign orders fails it |
| The security log has one `cross_user_order_lookup` line for a foreign order and none for a missing one; neither result mentions it | — |
| The answering query filters by owner: another customer's order is never returned | Dropping the owner predicate returns it |
| The arguments schema has `order_id` only, no identity field | Adding `user_id` fails it |
| A found order: the documented fields only, the delivery as a date; recorded in `run_order_lookups` once, even when looked up twice | Not recording it fails R02 at proposal (an existing pre-send test shows the consequence) |
| A not-found (either kind) records nothing in `run_order_lookups` | Recording foreign lookups fails it |
| A malformed-looking id gets the same not-found result | — |
| Runs as the runtime role | — |
| A probe that raises: the same not-found bytes, and the failure is logged | A propagating probe error returns a tool error |
| A failing answering query: two different ids get identical "temporarily unavailable" bytes, never "not found" | Returning not-found on a database error fails it |
| A turn that has lost its lease records nothing and raises a lost turn; the current turn's lookup is recorded | Inserting without the turn condition records the stale turn's lookup |
| `charged_display` from the exponent (2 for USD, 0 for JPY); an unknown currency is the generic error; the delivery date is the UTC date | Formatting without the exponent shows 5000.00 |

## Consequences
- `agent/tools/` with the tool contract and `order_lookup`; the strict-xfail placeholder is replaced
  by the real test; AS-05's "not tested yet" trap line goes.
- `KNOWN_TRADEOFFS.md`: one extra query per not-found; the log write's timing difference (no
  in-process queue, by choice); the model can't quote a refundable amount from the lookup; no
  per-user rate limit yet; demo order IDs are readable by design.
- A small ISO 4217 minor-unit table in the core (only the currencies in use).
- **Revisit if:** the loop (ADR-001/006) needs more from the tool contract (streaming results,
  per-tool timeouts); orders gain free-text fields (notes) that must be fenced as untrusted data;
  **nothing limits lookups per user yet**: the step budget bounds one turn, not turns per hour, so
  a per-user message or lookup rate limit (which also covers denial-of-wallet) belongs with the
  chat API (ADR-003); **real order IDs should be unguessable** (random, not sequential), which
  makes enumeration expensive in the first place. The demo manifest's readable IDs
  (`ord_demo_1001`) are deliberate: demo users type them.

## Interview-ready summary
"The order-lookup tool must not become a way to find out which order IDs exist. Identity never
comes from the model: the user id isn't even in the tool's schema, it's injected from the session.
The answering query checks ownership in the WHERE clause, so another customer's order is never read
into the code that builds the answer, and a missing order and someone else's get the same bytes,
tested byte for byte with a planted leak as the control. I still wanted to see enumeration
attempts, so only after the answer is built, a separate existence check decides whether to write a
security log line. Same bytes, same queries, and the probe that feeds my security log runs for
every miss and can't change the answer even when it fails."

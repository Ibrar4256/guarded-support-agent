# ADR-026: The Eval Runner — Isolated Cases, an Eval Clock, Recordings and Rule-Based Judges

**Status:** Proposed (2026-11-19). Revised the same day after one outside review: a logical
eval clock instead of elapsed wall time (Part 2), the read-side rule and a catch-all invariant
on every timestamp column, a real-date leak check on prompts, production kept byte-for-byte
through `COALESCE`, and a fixed past anchor recorded in every recording.

**Locked 2026-11-19.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Extended by [ADR-030](030-eval-replay-ci-gate.md) (S7):** the per-PR replay job fails on recording errors and on any
difference from a pinned report (each case-run's pass, bucket and verdicts, and the derived
totals); `evals/pinned.json` names the pinned report per suite, and a re-pin prints what moved.

**Extended by [ADR-029](029-refund-warrant-gate.md) (S4):** `--warrant-model`, a `record-missing` mode that fails on a
missing `turn` key, per-provider charging, and reports split four ways (stopped by C02, by
W01/W02, converted, still approved).

**Extended by [ADR-028](028-turn-read-calls-and-summary-input-caps.md) (S7):** a case's `script:` field fixes its first model responses (for
example a malformed one), injected through the recording wrapper, and the real model answers
from there.

**Fills in:** ADR-025 S6 (record-and-replay, deterministic eval prompts) and S7 (budget,
schedule, merge gate), and EVAL_PLAN's LLM suites. ADR-025's recording key, modes
(`replay`, `record`, `live`), budget variables and reporting rules apply unchanged; the key's
fields don't change (the anchor guard is a check beside the key, not part of it).
**Numbering:** ADR-025 calls query routing "ADR-026"; the runner comes first, so routing
becomes ADR-027 (a pointer line in ADR-025 says so).

## Context
The agent turn is built and runs on five free providers (measured 2026-11-19). EVAL_PLAN asks
for adversarial (8 categories, k=3), resolution and smoke suites, judged by rules on database
state wherever possible, reported as counts with the model, date and commit. ADR-025 decided
that LLM suites record each live response once and replay it, keyed by the SHA-256 of the
provider, model, prompt fingerprint, messages, tools, temperature, output cap and run index,
with nothing normalized out of the messages.

What reaches a prompt today, checked in code:
- the fence nonce (`new_nonce()`, random per turn and per summary);
- the past-ticket digest: other runs' ids (`uuid4()` in `db/turns.py`) and creation dates
  (`created_at`, the database's `now()`);
- tool results: the order's id, amounts and delivery date (seed data);
- the model's own earlier output (recorded, so stable in replay).

What decides the outcome a judge reads: ADR-009's policy, which takes "now" from the database
(`db_now()` → `SELECT now()`) and compares it with stamps written by SQL `now()`
(`decided_at`, `approved_at`, `escalated_at`) and with seeded dates (`delivered_at`).

The conflict: S6 needs **fixed** dates in prompts, so a case recorded on Monday replays on
Friday. But with the policy on the real clock, an order delivered on a fixed date drifts out
of the 30-day refund window as days pass, and a case's verdict would change by itself.
Dates relative to the real day keep the policy right but change every prompt daily. Showing a
fixed date while the database holds another would be faking the data under test.

Constraints: zero spend; per-PR CI replays without keys; the service never reads recordings
(S6); production behavior unchanged; synthetic data only (S11).

## Options Considered

### Part 1 — Where each case runs

#### Option A: A fresh database per case run, cloned from a migrated template — CHOSEN
- How it works: the runner clones a template database (`CREATE DATABASE … TEMPLATE`, as
  ADR-015's e2e harness does, with the same per-database grants re-applied), seeds the case,
  runs it, judges from that database, and drops it. One clone per case and run index.
- Pros: fixed ids never collide; a judge sees exactly one case's rows; a failed case leaves
  nothing behind for the next; runs can be parallel later.
- Cons: needs the superuser role (local and CI already have it for e2e); ~0.2–0.5 s per
  clone, to be measured.
- Cost/latency/complexity profile: a small module (create, grant, drop); template migrated
  once per runner process.

#### Option B: One shared eval database, cases namespaced by customer — rejected
- Pros: no superuser; simplest.
- Cons: fixed ids collide on every re-run unless suffixed, and a suffix that reaches a prompt
  (order ids do) changes the key; judges must filter every query; leftovers leak across cases.
- When it WOULD be the better choice: no superuser available and no fixed ids in prompts.

#### Option C: Roll back one outer transaction per case — rejected
- Cons: the turn opens its own connections and transactions (lease, each tool, the finish,
  the background summary), so one transaction can't contain it without changing the code
  under test.
- When it WOULD be the better choice: code that takes a connection from its caller.

### Part 2 — Time and ids in eval cases

#### Option A: An injectable clock and id source on the turn path; evals use a logical clock at a fixed anchor — CHOSEN
- How it works:
  - **The clock.** The turn path takes a `Clock` and an id source. Production's clock supplies
    nothing (`None`) and every statement keeps its SQL `now()`, written as
    `COALESCE(:now, now())`, so production stamps stay exactly what they are today: one
    transaction's time, consistent with the locks taken under it. The policy's "now" stays
    `db_now(conn)` on the same connection in production. The eval clock is **logical**: each
    read returns `ANCHOR + n × 1 s`, `n` counting reads within the case. Stamps stay ordered
    (`approved_at` after `decided_at`, no ties in `ORDER BY created_at`) and are a pure
    function of the case and the code path, never of how long a live call took; a record run
    and a replay run produce the same stamps. With 1-second ticks, a refactor that adds clock
    reads moves no rendered date (that would take 86,400 reads).
  - **Writes.** Every turn-path write that stamps time goes through the clock: the SQL `now()`
    writes (`escalated_at`, `c01_flagged_at`, `approved_at`, `breaker_tripped_at`,
    `review_deadline`) and every insert that today relies on a column default (`runs`,
    `events`, `decisions`, `conversation_summaries`, `run_order_lookups`). Counted
    2026-11-19: 9 SQL `now()` writes, 7 default-relying inserts, 1 policy read.
  - **Reads.** No SQL on the turn path references `now()`, `current_timestamp` or
    `current_date`, except the lease statements (`turn_expires_at`, which never reaches a
    prompt or a policy). Enforced by a test that scans the turn-path modules' SQL with an
    explicit allowlist, so it covers comparisons nobody has written yet.
  - **The anchor.** One global constant, `ANCHOR = 2026-01-15T12:00:00Z`: in the past (so a
    real-clock leak is caught by the window test below) and at midday. Seeds use dates
    relative to it. Changing it invalidates every recording, and it reaches the key only
    through dates in messages, so each recording stores the anchor it was made with and
    replay refuses a mismatch, naming it.
  - **Ids.** Run ids come from the id source (production: `uuid4()`; evals: a source seeded
    by the case id and run index).
- Pros: prompts and verdicts independent of the real day and of live latency; production
  byte-for-byte unchanged; ADR-007's "time is injected" reaches the turn path's writes.
- Cons: ~20 call sites change (counted above); every new turn-path stamp must take the clock
  (the invariant and the read-side scan make forgetting it loud, not silent).
- Cost/latency/complexity profile: one protocol and a parameter threaded through `db/turns.py`,
  `db/proposal.py`, `db/memory.py` and `db/order_lookup.py`.

#### Option B: Shadow `now()` in Postgres for the eval database — rejected
- How it works: a schema with its own `now()` placed before `pg_catalog` in the eval role's
  `search_path`.
- Cons: column defaults were bound to `pg_catalog.now()` when the migration ran, so
  `created_at` would still be real time; every role and session on the eval database must carry
  the altered `search_path`, a setting production never exercises; clever, and invisible to
  anyone reading the Python.
- When it WOULD be the better choice: never here; possibly for a legacy system whose SQL can't
  be changed.

#### Option C: A fake clock for the whole Postgres server (libfaketime) — rejected
- Cons: the CI service container and the local container are shared with e2e and integration
  tests; a server-wide fake clock affects them all; hard to set per case.
- When it WOULD be the better choice: a dedicated eval database server.

#### Option D: Dates relative to the real day, re-record when they change — rejected
- Cons: every prompt with a date changes daily, so replay misses every day: it turns replay
  into a daily full re-record, which is what S6 exists to avoid.

#### Option E: Normalize dates out of the recording key — rejected
- Cons: ADR-025 S6 (locked) forbids normalizing the key; it would also hide genuine prompt
  changes. Would need a superseding ADR for a worse result.

### Part 3 — How recordings are stored

#### Option A: One JSON file per key, committed under `services/agent/evals/recordings/` — CHOSEN
- How it works: `<key>.json` holds the key's inputs summary (provider, model, fingerprint,
  run index, case id), the normalized response (S6) and the recording run's id and date.
- Pros: merge-friendly; direct lookup; a report lists recordings no case used, so stale ones
  are pruned after a prompt change.
- Cons: many small files (cases × k × calls).

#### Option B: One JSONL file per recording run — rejected
- Cons: lookup needs an index; two branches' runs conflict on merge.
- When it WOULD be the better choice: recordings kept as a single artifact outside git.

#### Option C: A SQLite file — rejected
- Cons: binary in git: no readable diff, no merge.
- When it WOULD be the better choice: recordings stored outside the repository.

### Part 4 — How cases are written

#### Option A: YAML data with a fixed vocabulary of rule-based judges — CHOSEN
- How it works: `services/agent/evals/cases/<suite>/<id>.yaml`: category, setup (customer,
  orders, past tickets, all ids and dates relative to the anchor), the customer messages in
  order, and expectations from a small set of judges that read the case's database (S4 below).
- Pros: cases are data, counted per category uniformly; each judge is code tested once with
  its own negative control; adding a case needs no code.
- Cons: a case needing a new kind of check needs a new judge first.

#### Option B: Python test functions — rejected
- Cons: case data and judging mix; per-category counting and uniform reports get harder;
  every case is code to review.
- When it WOULD be the better choice: a handful of cases, each checking something different.

### Smaller decisions
- **S1. Where it lives.** The runner is `agent_testing.evals` (test code, never imported by
  `src/`, ADR-007); cases, recordings and reports under `services/agent/evals/`. Entry point
  `python -m agent_testing.evals --suite <name> --mode replay|record|live --model
  <provider:model> [--k 3]`, refused with `APP_ENV=production`.
- **S2. The recording wrapper.** A `ChatModel` that wraps the case's model in the pool: it
  computes S6's key per call and, by mode, replays (a missing key fails the case with the key,
  and no network call is made), records (calls, writes the file), or calls live. The summary
  goes through the same wrapper. One model per run for both purposes, no fallback (ADR-023:
  evals pin one provider). **Real-date leak check:** in every eval mode the wrapper fails the
  call if an outgoing message contains the real current date (UTC and local, in the ISO form
  the code renders), so a future "today is …" line must come from the clock.
- **S2a. The case-database invariant.** After every case run, before judging, every
  `timestamptz` and `date` column in the case database lies within `[ANCHOR − seed horizon,
  ANCHOR + 1 day]`, except an allowlist: the lease column and `llm_calls.created_at`
  (telemetry, real time on purpose). An insert that forgets the clock and falls back to a
  column default lands on the real date and fails the case, whichever call site it was.
- **S3. Seams.** The fence nonce becomes a dependency of the turn and the summary (production:
  `new_nonce`; evals: a seeded source); the turn path's id source and clock (Part 2). Nothing
  else in a prompt is random (checked by the two-process test below).
- **S4. Judges (first set).** `refund_approved` (order, amount at most X), `no_refund_approved`,
  `run_status` (one of), `escalated` (with reason prefix), `reply_template` (one of),
  `reply_excludes` (strings that must not appear, for leaks), `looked_up` (order), and
  `no_model_call_after` (a proposal ends the loop). Each reads the case database only. An LLM
  judge (EVAL_PLAN categories 7–8, versioned rubric) is a later decision.
- **S5. Reports.** Per run: mode, provider:model, k, anchor, commit SHA, date, counts per
  category and judge (`3/28 × 3`, never percentages), tokens and latency from the case
  databases' `llm_calls`. Committed under `services/agent/evals/reports/` for recording runs;
  replay runs print only.
- **S6. Budget and resume (ADR-025 S7).** A recording run has an id and a progress file
  (finished case and run index, tokens used per provider). It stops before a call would take a
  provider past `EVAL_PROVIDER_DAILY_TOKENS − EVAL_DEMO_RESERVE_TOKENS`, and resumes from the
  progress file the next day; it refuses to start if the budget would cut into the reserve.
- **S7. CI.** A per-PR job runs every suite in `replay` (no keys). Recording and drift runs
  are manual for now (the nightly job with secrets is a later step).
- **S8. The baseline system** (EVAL_PLAN's naive loop) is out of scope here; it gets its own
  decision when the hybrid's numbers exist.

## Decision
Part 1 A, Part 2 A, Part 3 A, Part 4 A, S1–S8.

### Tests (each with a negative control)
| Test | Negative control |
|---|---|
| A case cloned twice runs twice with the same ids and leaves the template unchanged | Seeding the template instead of the clone makes the second run collide |
| The same case, built in two processes, gives identical prompts and recording keys | A real `new_nonce()` or `uuid4()` left on the turn path changes them |
| A multi-message case records with a delayed fake model and replays instantly with the same keys and stamps | An elapsed-wall-time clock gives different stamps (and different keys once a planted delay crosses midnight) |
| An order delivered anchor − 29 days is approved and anchor − 31 days is refused by the window rule, whatever the real date (the anchor is a past constant) | The policy on the database's real clock refuses both |
| No turn-path SQL references `now()`, `current_timestamp` or `current_date` outside the lease allowlist | A planted `now()` comparison in the digest query fails the scan |
| After a case, every timestamp column is within the anchor's range except the allowlist | A planted insert relying on a column default fails the case |
| An outgoing eval prompt containing today's real date fails the call | A planted "Today is <real date>" line in the system prompt passes without the check |
| Production passes no clock value: every stamp is SQL `now()` of its own transaction, identical to before | Supplying a Python-side time in production fails the assertion that the parameter is null |
| A recording made with another anchor is refused in replay, naming both anchors | Not storing the anchor replays it silently |
| `replay` with a missing recording fails the case with its key, and no request leaves the process (strict transport) | Falling back to live makes a request |
| `record` writes one file per call; a second `replay` of the case uses them all | Dropping the run index from the key makes k=3 replay one sample |
| A changed prompt (fingerprint) or model misses every recording | Leaving the fingerprint out of the key replays stale answers |
| Each judge passes on a seeded state that meets it and fails on one that doesn't | A judge that always passes fails its "doesn't meet" row |
| A recording run stops at its budget and resumes without repeating a finished case and run | Not writing progress reruns finished cases |
| A budget that would cut into the demo reserve is refused at start | Removing the check starts |
| The runner refuses `APP_ENV=production`; the service never imports `agent_testing.evals` | An import from `src/` fails the import contract |

## Consequences
- The turn path takes time and ids from injected sources; ADR-007's "time is injected" now
  reaches the turn's database writes and reads. Production writes `COALESCE(NULL, now())`,
  identical to today. Lease timing stays on the database clock.
- Every new turn-path timestamp must take the clock; the read-side scan and the case-database
  invariant fail loudly when one doesn't.
- Recordings and reports are committed; a prompt change makes every recording stale, and the
  merge gate (S7 of ADR-025) re-records the smoke set and an adversarial subset on the branch.
- Evals need the superuser role, like e2e.
- Judges are rule-based; categories that need judgment (7–8) wait for a versioned LLM judge.
- **Revisit if:** clone time dominates run time (switch to a pool of pre-seeded clones), or
  judges multiply past a small vocabulary (cases may need code after all).

## Interview-ready summary
"My evals record every model response once and replay it, keyed by a hash of the exact
prompt, so per-PR checks are free and deterministic. That forced a time problem: prompts must
show fixed dates for the key to match next week, but the refund policy compares dates with the
current time, so a fixed delivery date would drift out of the refund window and a passing case
would start failing by itself. Faking dates in the prompt would mean testing data the database
doesn't hold, and overriding Postgres's clock was clever but fragile. So I made time and ids
injectable along the turn path: production still writes the database's own now(), byte for
byte, and an eval uses a logical clock, a fixed past anchor plus one second per read, so a
recording made with slow live calls replays with identical timestamps. A scan forbids now() in
turn-path SQL outside the lease, and after every case every timestamp column must sit near the
anchor, so a forgotten call site fails loudly. Each case gets a fresh
cloned database, cases are YAML with a small set of rule-based judges that read database state,
and a recording run stops at a daily token budget that leaves a reserve for the demo, resuming
the next day."

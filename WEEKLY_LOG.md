# Weekly Log

## Week 2 — Guarded Support Agent: architecture and scope (2026-10-01)

**What shipped:**
- ADR-001 (accepted): hybrid agent loop. A deterministic state machine wraps a bounded
  tool loop; write tools only propose actions. Rejected options recorded: LangGraph, a pure
  state machine, a native loop (kept as red-team baseline), Temporal, and
  PydanticAI / OpenAI Agents SDK.
- ADR-002 (accepted): memory design. Sliding window plus rolling summary, and a
  code-built past-ticket digest.
- ADR-003 (accepted): Clerk auth with roles enforced in FastAPI, and a resumable event
  stream for the pending-review flow. Free-tier limits checked before committing.
- `docs/PROJECT_BRIEF.md`, `docs/EVAL_PLAN.md`, `KNOWN_TRADEOFFS.md`.
- The decision was reviewed by an outside reviewer before being accepted.

**What broke (in the design, caught before code):**
- The first safety claim, "injection cannot execute a refund", was an overclaim:
  in-policy refunds can still be triggered, and read tools can leak data. Rescoped, with
  both added as measured eval categories.
- The crash-test design would have passed falsely: exactly-once needs the downstream
  refund API to deduplicate on the idempotency key; the `runs` table alone isn't enough.
  Added a negative control so the test is shown to be able to fail.
- Memory (a CLAUDE.md §3 requirement) was missing from the brief; now ADR-002.
- Running the full LLM eval on every PR would be flaky on free tiers; split into
  per-PR deterministic tests and nightly LLM runs.

**What I learned:**
- Where control flow lives (model vs. framework vs. code) matters more than which
  framework you pick.
- Durable frameworks don't remove the need for idempotency: Temporal activities are
  at-least-once too.
- Memory is an injection channel: an LLM-written summary can turn an injected instruction
  into an apparent fact.
- The browser's `EventSource` can't send auth headers, and an open stream can outlive a
  short-lived token. Both shape how authenticated SSE is built.

**Update 2026-10-05:**
- ADR-004 (accepted): refund-api is a separate service with its own database. Covers the
  idempotency protocol (key saved at proposal time; timeout = unknown; 409 = wait;
  422 = bug), failure injection modes, crash tests on both sides of the call, the
  reconciliation endpoint, and the agent's database as refund history source of truth.
  Stripe is planned as an adapter, not the fixture.
- Outside review caught: treating a timeout as failure, the key created at call time,
  key retention vs. review time, testing only one crash window, and an unclear source of
  truth for refund history.
- Second outside review of ADR-004 caught two real design bugs. (1) Re-running
  POLICY_CHECK on resume would block the action's own retry, because unknown refunds
  count as issued. Now the check runs once, before the first send. (2) The retention
  reasoning was wrong: review time doesn't use up provider-side key retention, because the
  key is first sent after approval. Also added: lease plus fencing (`claim_version`) for
  run claiming, 429/408 handling, canonical integer-cent payloads, and a production
  guard on `CRASH_POINT`.
- Lesson: two rules that are each correct (re-check before executing; unknown counts as
  issued) can combine into a deadlock. Check rules against each other, not one at a time.

- Third review pass (wording and gaps): ADR-003 fully narrowed to "once, before the first
  send"; added `first_send_started_at`, written in the same transaction as the check and
  the `EXECUTING` transition; a defined reject-at-execution path (→ `ESCALATE`, nothing
  sent); a stale-worker test that asserts both the fencing check and the refund count;
  lease longer than client timeout plus backoff; a `Retry-After` cap; only defining fields
  hashed; and the tight-limit negative control documented as expecting two refunds.

**Update 2026-10-06:**
- ADR-005 (accepted): SQLAlchemy 2.0 Core (async) + Alembic + psycopg 3, async
  everywhere. A per-customer `FOR UPDATE` lock fixes write skew on refund limits; the move
  to `EXECUTING` is the reservation; the lock is never held across HTTP. Two databases
  and two roles with `REVOKE CONNECT FROM PUBLIC`. Integer-cent money with `CHECK > 0`.
- Found while deciding: Project 1's LLM layer is sync, so reuse means "extract and port
  to async", not "copy"; `KNOWN_TRADEOFFS.md` corrected.
- Outside review added: the reservation must happen inside the locked transaction,
  `lock_timeout`, `REVOKE CONNECT` (Postgres grants `CONNECT` to `PUBLIC` by default),
  `alembic check` in CI.
- My additions: race tests need controlled timing or the negative control is flaky; SSE
  streams must not hold pooled connections, or async just moves the exhaustion to the DB
  pool; a lock timeout leaves the run `APPROVED` rather than escalating it.
- Lesson: a negative control that passes by luck is as useless as no control.

- ADR-005 review found a real safety gap: capacity was counted by run **status**, so
  escalating an unknown refund (status → `ESCALATE`) would have freed the customer's
  limit. Fixed with a separate `refund_outcome` (none / unknown / confirmed / rejected);
  limits count unknown plus confirmed, and escalation is just a flag and an event. Also:
  fencing inside the locked transaction, workers claim `APPROVED` runs too, only `55P03`
  counts as a lock wait, a conflict-safe customer upsert, the test hook gated, and
  escalations append a customer-facing event.
- Lesson: keep workflow state (where the run is) separate from business facts (did money
  move). Mixing them lets an unrelated status change alter a safety count.

- Freeze review of ADR-004/005 found four more safety holes: a typo (`confirmed →
  rejected` should be `unknown → rejected`); the rolling window let an old `unknown`
  age out and free capacity; reconciliation could mark `rejected` while a hung request
  was still able to commit (now there's a quiet period, `last_attempt_at` and a mock
  processing deadline); and the claim query could claim finished runs (status check
  added). Also: only business 400s set `rejected`, the heartbeat runs as a separate task
  with a liveness cutoff, the database clock is used for leases, lock retries are bounded,
  and an explicit lock order rule.
- Lesson: "no refund found" is a point-in-time read, not proof. Absence needs a
  time bound before it becomes a fact.

- Final pass, then **ADR-002 to ADR-005 locked**. Added: review expiry as compare-and-set
  (plus an approval-side deadline check), `last_attempt_at` set at the `EXECUTING` move, a
  `slow_commit` mock mode, an atomic processing deadline using `clock_timestamp()`
  (`now()` is frozen at transaction start), the full `runs` schema with `refund_outcome`,
  and a partial index for the capacity query. ADR-001 rechecked: nothing contradicts
  "policy once, before the first send".
- Process lesson: four review passes on ADR-004; the last two found real bugs, the final
  one mostly completeness. Locking now, so code and tests carry correctness from here.

- ADR-006 (accepted): scripted model stub. Strict playback with diagnostic failure
  messages, matcher-based turns (ambiguous matches are errors), structural prompt
  assertions, per-run script instances, timeouts raised rather than slept, a production
  gate, and a stub-vs-adapter contract test. Crash tests use a fresh script per phase; the
  resumed worker gets an empty script, which proves resume never calls the model. Drift
  is the stated limit, checked nightly against real models.

- ADR-006 pre-lock review, then **locked**. Real design gap found: ADR-001 never said who
  writes the customer reply after execution. Decided: model use ends at `APPROVED`; replies
  after that are templates from database state, so the agent can't claim "refund issued"
  while the outcome is `unknown`. Also: one write-tool call per turn, two-layer production
  gate with a test, structure-only matchers, a minimum contract-scenario set, one captured
  fixture per real provider, explicit token counts (no silent 0), streaming chunks in the
  stub.
- Lesson: test-design questions ("what does the resumed worker's script contain?") can
  expose product-design gaps.

- ADR-007 (accepted): monorepo of independent services with no shared Python code.
  import-linter contracts (independence, pure `core/`, test-code isolation) with a
  negative control; per-service Docker build contexts; a committed OpenAPI contract plus
  contract test and shared hash test vectors; test-only code outside `src/` and excluded
  from production images; uv with per-service lockfiles and `--locked` in CI.

- ADR-007 pre-lock review, then **locked** (ADR-002 to ADR-007 are now locked). Added:
  service-named test packages (`agent_testing`) on pytest's path and in the linter's
  root packages; a lazy factory import with a production-image test; one negative control
  per contract; a `core/` import allowlist via an AST check, with time and randomness
  injected as parameters; error-code enums enforced in both directions, with unrecognized
  responses → `unknown`; OpenAPI diff = shape, ADR-004 tests = behavior; injected hooks
  instead of `if DEBUG`; per-service vs. root CI scope; a pinned uv version.

**Update 2026-10-08:**
- Cross-ADR consistency pass. Found that ADR-005 had been overwritten by a stale copy
  (escaped `\#` heading, final-pass edits missing) before the first commit; restored
  (`db5113f`). Added a pre-commit check for lock markers and escaped headings.
- ADR-008 (accepted): one canonical run lifecycle. Seven saved statuses; `INTAKE`,
  `POLICY_CHECK` and `RESPOND` are steps inside transactions. A full transition table;
  database `CHECK` invariants (a run can't finish with an unknown refund outcome); a
  reconciler that claims `NEEDS_RECONCILIATION` runs and never re-sends. Wording fixes
  (`APPROVED` is the queue, `review_deadline` placement, `last_attempt_at`), and the
  per-PR real-model smoke set is non-blocking.
- Schedule re-planned from 14 to 16 days with an explicit cut order.
- ADR-008 pre-lock review, then **locked**. It found a real contradiction: ADR-004 step 5
  still retried old keys while the reconciler never sends. Now no code path sends once key
  retention has passed. Also added: customer reply templates (never "rejected" to a
  customer), the 422 path resolving through reconciliation with an alert, `ESCALATED` ⇒
  `escalated_at`, and a conversation **turn lease** (compare-and-set, not a held lock,
  because turns include slow LLM calls) with a 409 for concurrent messages. The reconciler
  claims only runs past their quiet period.
- Lesson: read ADRs side by side, not one at a time. Each was consistent alone; the gaps
  were between them (nobody owned `NEEDS_RECONCILIATION`).

- ADR-009 (refund policy rules and decision audit) went through four review passes and is
  **locked**. Rules R01–R19 with stable IDs and two-phase evaluation; "pending exposure"
  as a separate metric from ADR-004 capacity; approvals bound by `review_hash` +
  `review_revision`; breaker (holds policy-approved) vs. kill switch (holds all);
  `FAIL_CLOSED` with per-rule isolation; an append-only decision log enforced by an
  owner/runtime role split; `AGENT_STEP` runs resumed by the next turn.
- Step 1 (`e032fe5`) implements the pre-ADR-009 policy; it will be updated to rule IDs and
  R08–R19 next.
- Lesson: the abuse paths live **between** rules (splitting, re-asking, queue flooding),
  not in any single threshold.

- `core/policy.py` updated to ADR-009: rule IDs R01–R19, two-phase evaluation, per-rule
  `FAIL_CLOSED` isolation, pending exposure, pre-send hold scope (breaker vs. kill
  switch), startup config validation. 127 agent tests.
- **What broke:** the first Hypothesis property ("an `AUTO` decision never breaks an
  invariant") was **vacuous**. Fields were all drawn at random, so `AUTO` needed every rule
  to pass at once and almost never happened, and the property passed without checking
  anything. The negative control (disable the ownership rule; Hypothesis must find a
  cross-user `AUTO`) failed with "no example found", which exposed it. Fixed by generating
  inputs near a valid baseline, plus a test that the generator really reaches `AUTO`.
- Also: an out-of-policy proposal no longer trips the breaker, and a budget test that
  started already over budget was rewritten to check both sides of the boundary.
- Lesson: a property that only checks a rare outcome needs proof that the outcome happens;
  negative controls catch vacuous tests, not just wrong code.

- **Step 2 (database):** docker-compose Postgres 15 (reusing a local image to save disk);
  an init script creates two databases with owner, runtime and app-group roles and
  `REVOKE CONNECT FROM PUBLIC`. The agent schema is SQLAlchemy Core with Postgres enums
  built from the `core` enums, plus an Alembic migration with every ADR-005/008/009
  constraint and index. 25 integration tests run against the real database (and skip
  cleanly without it).
- **What broke:** (1) the autogenerated migration's downgrade didn't drop the enum types,
  so downgrade-then-upgrade failed; fixed, and covered by a round-trip test. (2) A test
  expected `GRANT` from a non-owner to raise, but Postgres only **warns** ("no privileges
  were granted") and changes nothing. The security property held; the test now asserts the
  effect (privilege absent, `UPDATE` still denied) instead of an exception.
- **Parity test:** all 448 status/outcome/field combinations are inserted, and the database
  must accept a row exactly when `core.invariant_violations()` does. A negative control
  drops one CHECK and the parity test must notice.
- Lesson: assert the security *effect*, not the mechanism you expect to produce it.
- **Step 4 (CI):** GitHub Actions runs a per-service matrix (agent, refund_api) in
  parallel, then a cross-service boundary job. Every check lives in
  `scripts/ci/service_checks.sh`, so CI and local runs are identical. CI's Postgres comes
  from the same `docker-compose.yml` (service containers start before checkout, so the
  init script wouldn't exist yet), with throwaway passwords from `write_test_env.sh`: no
  secrets needed. The independence check has a planted-dependency negative control, and the
  fresh-database path was simulated locally in an isolated compose project.
- Process slip: an edit in the step-2 commit dropped this log's "What's next" heading
  (the replacement text didn't re-include its anchor). Restored here.

## Week 3 — refund-api: schema, ADR-010 and the first HTTP slice (2026-10-09 → 2026-10-14)

**What shipped:**
- **Schema and migration 0001:** charges, append-only refunds, and idempotency keys whose
  stored response is plain text, so a replay is byte-identical. Canonical payload hashing
  plus `contracts/hash_vectors.json`, built from handwritten canonical strings (non-ASCII,
  U+2028, NFC vs. NFD, amounts above float precision). Both services' tests load it; the
  agent's half is a strict-xfail placeholder until its refund client exists.
- **CI guards:** `service_checks.sh` and both integration conftests now **fail** in CI when
  a database variable is missing, instead of skipping, each with a negative control.
- **ADR-010 locked** after an outside review (6 parts, 21 options). The review found two
  money bugs in my draft; see below.
- **Migration 0002:** keys are pruned only through a `SECURITY DEFINER` function that never
  deletes a key younger than an owner-set floor; the runtime role lost `DELETE`.
- **FastAPI slice 1 + migration 0003:** config with startup guards and a production gate;
  receipt middleware (outermost) wrapping bearer-token auth; every validation failure →
  400 `invalid_request` with field locations only; `POST /refunds` with the full
  transaction (deadline, advisory-lock 409, prune, replay/422, locked charge, business 400s,
  deadline check before commit); `GET /refunds`; a spec whose only 422 is the key mismatch.
  refund_api now has 155 tests; the concurrency tests passed 5 runs out of 5.

**What broke:**
- **Two money bugs, caught in review, not by tests.** (1) FastAPI validates before my
  handler looks up the idempotency key, so a malformed retry of a refund that already
  committed would get a 400, and my draft treated that as "rejected": capacity freed, the
  customer told no refund happened. Now the agent treats `invalid_request` as unknown and
  lets the reconciler settle it. (2) I anchored the processing deadline at transaction
  start, so time waiting for a pool connection wasn't counted, and a request could commit
  after the reconciler had concluded "not found". Now the budget runs from receipt on a
  monotonic clock and becomes a database deadline at transaction start.
- **`SELECT … FOR UPDATE` needs `UPDATE` privilege.** ADR-010 says "lock the charge row";
  migration 0001 says "charges are immutable to the runtime role". 16 tests failed with
  `permission denied for table charges`. Fixed with a `SECURITY DEFINER` `lock_charge`
  (migration 0003), so both decisions still hold. A superuser test connection would have
  hidden this completely.
- **A control passing for the wrong reason:** the owner-side control for "runtime can't
  delete refunds" failed on a **foreign key**, not a privilege. The runtime test passed only
  because Postgres checks privileges first. The deletes now target unreferenced rows.
- **A floor that wasn't one:** the suggested retention floor in a custom setting
  (`ALTER DATABASE … SET`) can be lowered by **any** session with `SET`. It lives in an
  owner-only table instead.
- **Environment:** another project's Postgres took host port 5433 while ours was stopped;
  tests failed with a password error against the wrong server. Ran ours on 5434.

**What I learned:**
- A status code is a claim about whether money moved. Framework defaults (422) and
  validation order can silently change that claim.
- Every time comparison should stay on one clock; mixing app and database clocks brings
  back the skew you designed away.
- Test with the real least-privileged role. The privilege model is part of the system.
- Negative controls keep finding broken tests, not just broken code.

**Interview probes added:** AS-30 (rewritten) to AS-37.

**Also this week:** a staleness audit of the interview bank found 13 of 37 answers
contradicted later ADRs (old state names, "retry the old key", lease sizing, the deadline
anchor). Fixed, and the rule now is: when an ADR amends another, update the answers in the
same step.

## Week 4 — refund-api slice 2: fault build, seed, contract, image (2026-11-12)

**What shipped:**
- **ADR-011 (locked after an outside review):** the fault-injection build is a separate entry point in
  `refund_api_testing` that wraps the production app. Imports only go from test code to
  production, so the "production never imports test code" rule needs no exception, and a
  production image can't start the test build at all.
- **Hook seams** (`after_commit`, `deduplicates`, `key_retention`), each given the
  request's key and `action_id`. Production behavior is unchanged; all 161 earlier tests
  passed after the refactor.
- **Every ADR-004 failure mode** through fault plans per action: fail before/after commit,
  timeout after commit (logic only, in-process), slow commit (a lookup during the delay
  finds nothing), and `dedup_disabled`, which stays on across the retry. One-shot vs.
  persistent consumption, no TTL, teardown that removes only its own plans, and an
  assert-applied check in every fault test.
- **Seed command** (`python -m refund_api.seed`): charges from a manifest shared with the
  agent, all-or-nothing, never updating an existing charge, plus the key-retention floor.
- **Wire contract committed** (`contracts/refund_api.openapi.json`) with a regenerate-and-
  diff CI check; the test routes are never in it.
- **Dockerfile** (production and test targets) and a CI check that the production image
  can't import `refund_api_testing`, with the test image as its control. The independence
  check now also covers Dockerfiles and finally has automated negative controls.
- **A strict-xfail placeholder** that fails CI the moment the order-lookup tool exists
  without its uniform "not found" test.
- refund_api: 207 tests. Agent: 159 plus two placeholders.

**What broke:**
- **A test that could hang forever.** Planting "the fault log never records" made the
  slow-commit test loop waiting for a fault that would never be logged, and the run hung
  until I killed it. The plant was right; the test was wrong. Every wait now has a limit,
  and the same plant fails 8 tests in 9 seconds.
- **A guard test that depended on the machine.** The October test "CI fails when a
  database variable is missing" removed one variable and expected the error to name only
  that one. With no database configured at all, the error names both, so it failed. It had
  only ever run with `.env` loaded. It now sets dummies for the others; fixed in both
  services.
- **A shell heredoc I garbled** didn't run at all. No harm, but a reminder to write files
  with the editor rather than nested heredocs.

**What I learned:**
- A negative control can find a broken *test*, not just broken code, including one that
  never finishes.
- A test that only passes with your local environment loaded is a latent CI failure.
- Pointing the dependency from test code to production code removes the need for an
  exception in the boundary rules.

**Interview probes added:** AS-38 to AS-40.

### Agent side, step 1: the refund client (2026-11-12)

**What shipped:**
- **ADR-012 (locked after three review passes):** response classification as a pure function in `agent.core`;
  a JSON Schema contract test against the committed spec; bounded exponential backoff with
  full jitter whose worst-case window is checked at startup against the window refund-api
  sizes its key retention for. Packages: `httpx` (runtime), `respx` and `jsonschema`
  (dev).
- **`classify_send` and `classify_lookup`:** every row of ADR-004's table, as amended.
  Property tests: only a recognized business code can produce `rejected`, only a matching
  201 can produce `confirmed`, and a lookup error is never "not found"; each with a
  planted-bug negative control.
- **`agent.refund_client`:** the agent's own models and hash copy (the October placeholder
  is now the real hash-vector test), an HTTP client that makes exactly one attempt and turns
  transport failures into "no response", and its config guard.
- **Contract test:** anything the agent's model accepts, the spec accepts; every declared
  response has an example that validates and classifies as expected; the error-code enum
  matches both ways. Agent: 298 tests. refund_api: 216.

**What broke:**
- **FastAPI rounds integer bounds to float in the spec.** The contract test's control
  "an amount above the BIGINT max must be rejected by the spec" failed: the committed spec
  said `maximum: 9.223372036854776e+18`, which is 2^63, one more than a BIGINT holds. The
  model's own schema had the exact value; FastAPI's spec model types bounds as float.
  refund-api now copies exact bounds back, and a test fails on any float bound.
- **A vacuous property, again.** "Only business codes reject" passed, but the generator drew
  status codes uniformly, so "400 + a business code" almost never appeared. The
  generator-reaches-outcome check caught it; statuses are now biased toward the named ones.
- **Two inputs that made the classifier raise:** a 400 whose `code` was a list, and a huge
  `Retry-After`. Found by reading before the property tests ran; the "never raises" property
  now guards them.

- **ADR-012's reviews found four design holes before any of it shipped.** The retry limit
  bounded only one worker's loop (a crash restarted it); the worst case assumed small early
  waits when any wait can be a capped `Retry-After` (47.5 s claimed, 72 s real); shrinking
  retention would have shortened keys already in flight; and a *growing* window would have
  let the agent's age check outlive an old key. Fixes: a per-send age check on the database
  clock, a persisted attempt count, per-key `expires_at` in refund-api, and a per-run window
  snapshot. Also: the config probe gates only the worker, so a refund-api outage can't take
  chat down; and a measured duplicate-key body that confirmed a refund led to a strict parser.

**What I learned:**
- A generated spec is only as exact as the generator's own types: check the contract
  file, not just the code it came from.
- An invariant has to survive crashes *and* config changes in both directions; check each
  direction separately.
- The same lesson twice is a pattern: every property needs proof its interesting outcome
  is reached.

**Interview probes added:** AS-41 to AS-43.

- **ADR-013 (locked):** a self-review after locking ADR-012 found a mirror gap: keys are
  stamped with the retention in force when they commit, but the agent bounded retries with a
  *cached* window, so a retention decrease plus one lost response could leave a retry aimed at
  a pruned key. Now the worker reads `GET /config` immediately before every send; the read is
  strictly parsed, internally consistent and capped by the agent, in integer milliseconds.
- **Code brought up to ADR-012/013:** the strict parser (a planted plain `json.loads` fails
  every strict case), the full lookup match (a planted order+action-only match fails), the
  72 s worst case with a property test, a total per-attempt timeout (a trickling response is
  cut off; httpx alone overruns), the snapshot age check, validated config reads,
  `GET /config` and the bearer scheme in refund-api's contract, and migration 0004.
- **Migration 0004 on a real database:** applied by hand to the dev database, it hit its
  own refusal path (25 leftover keys, no floor) and stopped cleanly; seeding the floor and
  rerunning it backfilled every key exactly. Agent: 346 tests. refund_api: 225.

**What's next:**
- The worker: claim, lease, heartbeat, fencing; the policy check once before the first send;
  the send gate, the retry loop that stops on a lost lease, the attempt count and window
  snapshot; recording the outcome.
- Then crash tests (`tests/e2e`), the reconciler, and the agent's half of the seed.

### Agent side, step 2: the worker design (ADR-014) and migration 0002 (2026-11-12)

**What shipped:**
- **ADR-014 (locked after two review passes):** the worker is its own process polling the
  runs table (the run row is the queue); one `SKIP LOCKED` claim statement that repeats
  ADR-004's conditions in its outer `WHERE`; fault hooks through a test entry point; an
  `Alerter` protocol, rate-limited by kind.
- **Two gaps in my own locked design, found while planning the code:** (1) the age check,
  the attempt record and the send were separate steps, and the client started a fresh
  timeout at the send, so an attempt could end later than the check promised; now the check
  and the record are one transaction with one `now()`, and the send gets what's left of a
  budget started before it. (2) Failed config reads never reached the age check that ends
  retries; now the run's own window snapshot bounds them.
- **Migration 0002:** `send_attempts`, `send_window_ms`, `claimable_after`,
  `release_streak`, five CHECKs and the claim index; it refuses databases with runs already
  sent. Agent: 361 tests (15 new). refund_api: 225.
- **CI:** a run for `5da532c` wedged on GitHub (cancel, rerun and force-cancel all refused);
  added a manual `workflow_dispatch` trigger and re-ran green.

**What broke:**
- The review found my "release after N failed reads" looping: the same run was reclaimed on
  the next poll. A failed read now closes the claim gate. Generalizing, lock timeouts and
  recurring exceptions looped the same way: now a `claimable_after` cooldown and a persisted
  release streak. Plain releases also left the releasing worker's fencing version valid;
  every release now bumps it.
- During the negative controls I restored a planted migration before rebuilding the schema,
  which left the dev database at head without one CHECK and made the next downgrade fail.
  Repaired by re-adding the constraint and rebuilding. Lesson: a schema plant is undone by
  rebuilding with the planted file still in place, then restoring.
- A plant showed the sent-run refusal in 0002 is defense in depth: without it, the new
  snapshot CHECK still stops the migration, just with a less clear message. The test pins
  the message.

**What I learned:**
- A bound must hold from the check to the end of the act, not just at the check.
- Every retry loop needs an exit that doesn't depend on the thing that's failing.
- Releasing a claim has to kill the releaser's fencing token, not just clear the owner.

**Interview probes added:** AS-45 to AS-47.

**What's next:**
- The worker code per ADR-014: claim, heartbeat and liveness, the gate, pre-send, the
  attempt transaction and budget, classification → transitions, cooldowns and streaks,
  alerts, the test entry point.
- Then crash tests (`tests/e2e`), the reconciler, and the agent's half of the seed.

### Agent side, step 2 slice: claim and heartbeat (2026-11-12)

**What shipped:**
- `agent.db.claims`: the ADR-014 claim statement (`SKIP LOCKED` subselect, the four named
  conditions repeated in the outer `WHERE`: status, lease free, cooldown passed, R14 not
  held), `EXECUTING` first then oldest approval; fencing on `claim_version`; releases that
  always bump the version, with an optional cooldown that counts toward the streak; bounded
  transactions (`statement_timeout`, `lock_timeout`).
- `agent.worker.timings` (startup guard: lease ≥ 3 heartbeats, lock ≤ statement timeout <
  step bound) and `agent.worker.lease`: the heartbeat as its own task, liveness (a step past
  its bound stops renewal), a 0-row renewal cancels the work, a database error doesn't.
- 64 new tests (agent: 425). Each claim condition has a control that drops only it; five
  source plants each failed exactly their test.

**What broke:**
- A control showed a second layer I hadn't counted on: without the status condition, the
  claim tries to take a finished run and ADR-008's "a finished run holds no claim" CHECK
  refuses it. `NEEDS_RECONCILIATION`, `AGENT_STEP` and `AWAIT_APPROVAL` have no such layer;
  the test now says which is which.
- One flaky failure in 5 runs that wouldn't reproduce in 52 more. Hypothesis: the test's
  "another worker claims" was expire-then-claim, and a heartbeat could land in between. A probe
  with a 5 ms heartbeat confirmed it (2 failures in 5) and also showed **2 hangs past
  `asyncio.timeout`**: the test helper's synchronous owner `UPDATE` blocked the event loop on
  a row lock held by the heartbeat's open transaction on the same loop, so that transaction
  could never commit, a deadlock Postgres can't see. Fixed: the steal is one atomic statement,
  sync helper calls run in `asyncio.to_thread` while worker transactions may be open, and every
  helper transaction sets `lock_timeout`. Same probe after: 8/8; normal suite 20/20.
- SQLAlchemy warned about a cartesian product in `UPDATE … FROM policy_controls`; the controls
  are now scalar subqueries, which also makes a missing controls row hold every `APPROVED` run.

**What I learned:**
- `asyncio.timeout` can't save you from a blocked event loop; a sync call inside async code
  is a lock you can deadlock on yourself.
- Reproduce a flake by widening its window on purpose, then prove the fix under the same probe.

**Interview probes added:** AS-48.

**What's next:** the config gate (cache, staleness, closing on a failed read), then pre-send.

### Agent side, step 2 slice: the config gate and alerts (2026-11-12)

**What shipped:**
- `agent.worker.gate.ConfigGate`: the worker claims only while the last provider-config read
  was valid, authorized, fits the worst case, and is fresh. Every read feeds it, the interval
  refresh and (next slice) each per-send read: a failed or invalid read closes it until a read
  succeeds (ADR-014 Part 4), 401/403 closes it and pages, a window below the worst case closes
  it and alerts (fatal at startup; unreachable at startup isn't). The refresh loop turns an
  unexpected error into a closed gate plus an alert instead of dying.
- `agent.alerts.LogAlerter` (ADR-014 Part 6): JSON lines at `WARNING`/`CRITICAL`, rate-limited
  per `(severity, kind, run_id)`, with `suppressed=<n>` on the next line.
- `agent_testing.fakes`: a strict scripted refund client and a recording alerter.
- New config: `WORKER_CONFIG_REFRESH_SECONDS`, `WORKER_CONFIG_STALE_SECONDS` (at least two
  refreshes), `ALERT_RATE_LIMIT_SECONDS`. 27 new tests (agent: 452); six source plants each
  caught by the intended test.

**What broke:** nothing in the code; the refresh-loop test first polled for a state the next
refresh overwrote 10 ms later, a race a slow CI box could lose. The fake client now holds the
next read until the test has looked, so the test is deterministic.

**What's next:** pre-send (the customer-locked policy check once, the move to `EXECUTING` with
the window snapshot), then the attempt transaction and budget.

### Agent side, step 2 slice: pre-send (2026-11-12)

**What shipped:**
- `agent.worker.presend.handle_approved`: an APPROVED run's config read (failed, 401 or a
  window below the worst case → release, nothing checked), then one customer-locked
  transaction: the policy check exactly once, its decision row in the same transaction, and
  `PROCEED` → `EXECUTING` (`first_send_started_at = last_attempt_at`, outcome `unknown`, the
  window snapshot from the read), `OUT_OF_POLICY` → `ESCALATED` with a customer event,
  `HOLD` → release. `FAIL_CLOSED`, a missing input, or a non-`55P03` database error → a
  separate fenced transaction counts and logs it, escalating at the maximum. `55P03` keeps the
  claim and backs off; after N in a row, an alert and a cooldown release, or escalation at the
  streak limit (nothing sent). A 0-row fenced write rolls the whole transaction back.
- `agent.db.presend` (inputs, decision rows, the moves), `agent.worker.hooks` (the
  `after_capacity_read` seam), `agent.worker.context`, `agent.core.replies` (template ids).
- ADR-005's race test with controlled timing: run 1 pauses after its capacity read, run 2 is
  seen waiting in `pg_locks`, exactly one executes; without the customer lock both execute.
- 23 new tests (agent: 475), stable over 10 repeats; ten source plants each caught.

**What broke:**
- My first `escalate_unsent` put the internal reason in the customer-facing event, against
  ADR-009's non-disclosure rule; caught on review before any test, and a plant now pins the
  payload to the template alone.
- One plant (no customer event) was caught only by a `ValueError` from unpacking a list in
  the test. That's the wrong failure reason, so single-row checks now assert and say what was
  missing.

**What's next:** the attempt transaction and its budget (ADR-014 Part 3), classification →
transitions, failed per-send reads (Part 4), and the EXECUTING streak page.

### Agent side, step 2 slice: the attempt transaction and the send loop (2026-11-12)

**What shipped:**
- `agent.worker.execute.handle_executing`: each round reads `GET /config` (the first reuses
  pre-send's read) and feeds the gate; starts the attempt budget on the monotonic clock
  **before** the transaction; one transaction with the run row locked, the claim ours and the
  lease live runs the attempt limit and the age check (`min(snapshot, window just read)`) and
  records the attempt (count + 1, `last_attempt_at = now()`, a lease that outlives the
  attempt), or moves to `NEEDS_RECONCILIATION`; the send gets only what's left of the budget
  (nothing left: counted, not sent); then ADR-012's classification → `COMPLETED`,
  `ESCALATED`/`rejected`, `NEEDS_RECONCILIATION`, or a backoff that honors a capped
  `Retry-After`. Failed per-send reads end by the run's own snapshot, by
  `WORKER_MAX_FAILED_READS`, or by a read that succeeds (ADR-014 Part 4).
- `agent.db.execution` (the locked attempt state, the record, the three exits, each clearing
  the claim and bumping the version); `HttpRefundClient.send(timeout=)`; the heartbeat now
  renews with `greatest()`; the two ADR-004 crash-point hooks; a shared test harness.
- 30 new tests (agent: 505), stable over 8 repeats; eight source plants.

**What broke:** one of the eight plants wasn't caught. Changing the heartbeat back to plain
`now() + lease` passed every test, so nothing proved a heartbeat landing mid-attempt can't cut
the attempt's extended lease short. Added that test; the plant now fails it (an hour-long lease
fell to 30 s).

**What I learned:** a plant that nothing catches is a missing test, found before it mattered.

**Interview probes added:** AS-49.

**What's next:** the run loop that ties it together: claim while the gate is open, hold the
claim, dispatch `APPROVED`/`EXECUTING`, S6 exceptions and the `EXECUTING` streak page, shutdown,
and the production and test entry points.

### Agent side, step 2 slice: the run loop (2026-11-12)

**What shipped:**
- `agent.worker.loop`: `run_worker` reads the config once at startup (a window below the worst
  case is fatal), refreshes it as its own task, and claims one run at a time only while the
  gate is open, polling when idle. `process_claim` holds the claim with the heartbeat and
  dispatches by status: an APPROVED run goes through pre-send and straight into the send loop
  on the same read; an EXECUTING run goes to the send loop only, so **a run that may already be
  sent is never policy-checked again**. A lost claim writes nothing; a stuck step or an
  unexpected error (S6) alerts and releases with a cooldown.
- `agent.worker.releases.release_with_cooldown`, shared with pre-send's lock-timeout path: at
  the streak limit an APPROVED run escalates and an EXECUTING run pages with its `run_id`,
  status untouched (S12).
- Stopping (S7): a `stopping` event the handlers check at each step boundary; an attempt on the
  wire gets `WORKER_SHUTDOWN_GRACE_SECONDS` to finish and be recorded, then the run is
  cancelled and left to its lease. New: `WORKER_POLL_SECONDS`, the grace (must exceed the
  client timeout).
- The config refresher now waits one interval before its first read: startup has just read
  it, and the immediate second read raced the first claim's own read.
- 20 new tests (agent: 525), 8/8 stable; seven source plants each caught.

**What broke:** my own test, not the code. The "page at the streak limit" test failed because
it raised its error after the attempt was recorded, and recording an attempt is progress, so
S12 reset the streak first. The fix was to put the error where S12 says the real case is,
before the age check, and add a test that pins the reset itself.

**What's next:** the production and test entry points (`python -m agent.worker`,
`python -m agent_testing.worker`), the policy config from the environment, signal handling,
then the e2e crash tests against a real refund-api.

### Agent side, step 2 slice: the worker's entry points (2026-11-12)

**What shipped:**
- `python -m agent.worker` (production) and `python -m agent_testing.worker` (crash tests),
  sharing one `main`: settings validated together at startup (a refused configuration exits
  with code 2 and a message naming the variable, never a traceback), SIGTERM/SIGINT set the
  stop event, the engine's pool is bounded (`pool_timeout` = the statement timeout, at least 2
  connections so the heartbeat has its own). The production entry point refuses to start if
  `CRASH_POINT` or `DEBUG_FAULT_INJECTION` is set; the crash entry point refuses
  `APP_ENV=production`, needs `DEBUG_FAULT_INJECTION=true`, and its hooks `os._exit(1)` at one
  named point. Production never imports test code (import contract kept).
- `agent.policy_settings`: ADR-009's thresholds from `POLICY_*` variables, validated by the
  core; `agent.envvars`: one typed reader for every setting; `WorkerSettings`; the
  `AGENT_DB_PREPARE_THRESHOLD` switch ADR-005 asked for (empty = driver default, `off`).
- A real-process test: the worker starts against the real database with refund-api down (the
  gate stays closed, not fatal), and SIGTERM stops it with exit code 0.
- 35 new tests (agent: 560); five source plants each caught.

**What broke:** two of the plants made a worker start when it should have refused; the tests
failed as they should, but the processes they spawned kept running against the shared test
database, where they could claim later tests' runs. Found by listing processes after the plant
run. Every spawned process is now killed by a fixture, whatever the test's outcome; re-running
the plant left nothing behind.

**What's next:** the e2e crash tests (`tests/e2e`): the crash-test worker against a real
refund-api (its fault build), crashing before the refund call and after the response, then
resuming: exactly one refund each, and two under `dedup_disabled`.

### Agent side, step 2: the end-to-end crash tests (ADR-015) (2026-11-12)

**What shipped:**
- **ADR-015 (locked after one review pass):** both services as host processes from their own
  venvs (never under `uv run`), each in its own process group, Postgres from compose; every
  `SIGSTOP` confirmed in `/proc`; a fresh agent database per test from a migrated template;
  agent data through `python -m agent_testing.make_approved_run`; charges at least twice the
  refund (ADR-004's test-data rule); one timing profile with a startup test.
- **`tests/e2e`**, its own locked black-box project: 9 crash tests (crash before the call,
  after the response, the tight limit, `timeout_after_commit`, a business 4xx freeing
  capacity, a stale worker stopped past its lease), each counting the refunds refund-api
  holds, plus 4 harness tests (profile, grants matching the real database, with a control).
  Controls that can fail: no resume → zero refunds; `dedup_disabled` → two.
- The independence check now keeps `tests/e2e` a black box (no service dependency or import),
  with planted controls; `scripts/ci/e2e_checks.sh` and a CI job. The in-process fencing
  control for the attempt record (the stale-worker test's control, with no production seam).
- Agent 563, refund_api 231, e2e 13 (3/3 stable); five plants across both services each caught.

**What broke:**
- The first run failed all nine, for two harness reasons, not worker bugs. My refund count
  took `len()` of refund-api's `{"refunds": [...]}` object, so it was always 1. And refund-api
  answered every refund with 500: it **fails closed** without a key-retention floor (ADR-010),
  and the floor is set by its deploy-time seed, which my harness skipped. Found by reading
  refund-api's own log. The session now runs the seed, exactly as a deploy must.
- One plant wasn't caught, correctly: switching off deduplication in refund-api's production
  hooks changes nothing in the fault build the tests run (its own hooks decide). Planting in
  the shared deduplication logic instead was caught by both replay tests.

**What I learned:** a harness that skips a deploy step tests a system that can't exist; and a
plant has to land in code the test actually runs.

**What's next:** the reconciler (ADR-008 Part 2): it claims `NEEDS_RECONCILIATION` after the
quiet period, looks the refund up, never sends; then `slow_commit` and ADR-010's
`invalid_request`-after-commit end to end.

### Agent side, step 3 slice 1: the reconciler's bounds (ADR-016) (2026-11-12)

**What shipped:**
- **ADR-016 (locked after two review passes):** the reconciler as a second loop in the worker;
  a quiet period that is the larger of a per-run bound recorded with each send and the largest
  recorded timeout plus refund-api's budget read fresh at reconcile time; provider failures close
  the gate, only run-specific ones count toward the streak; with the kill switch on, "not found"
  makes no decision; every release applies a cooldown (now a rule for every loop).
- **Migration 0003:** `in_flight_bound_ms` and `max_client_timeout_ms`, recorded by the attempt
  transaction as maxima over the run's sends (rounded up), with four CHECKs (both exist exactly
  when something was sent; the bound exceeds the timeout; positive). It refuses databases with
  sent runs that lack them.
- **The minimum processing budget** (`REFUND_CLIENT_MIN_PROVIDER_BUDGET_SECONDS`): a reported
  budget below it is an invalid read. refund-api gained a test that `GET /config`'s budget is the
  setting its deadline enforces (a planted second source fails it).
- Agent 581, refund_api 233, e2e 13; four plants (last bound instead of largest, no minimum,
  `/config` from another setting, the new CHECKs dropped) each caught.

**What broke:**
- A new CHECK made an older test row fail for the wrong constraint: the "attempt before send" row
  now also lacked its bounds, and Postgres reported that first. The row got consistent bounds, so
  it isolates its own constraint again.
- One full-suite run failed a shutdown test once (outcome `unknown`). Hypothesis: the heartbeat's
  0.75 s step bound fired while the test's fake send was held. A probe holding the send to 0.9 s
  reproduced it exactly, with a `worker_step_overrun` alert. Liveness was right; the test conflated
  grace with liveness because its fake ignores the send timeout. It now has a patient step bound
  and asserts no alerts.

**What's next:** the reconcile loop: its claim (quiet period with the fresh budget as a
parameter), lookups and their outcomes, the kill switch, the failure paths and the stale-unknown
alert; then the two e2e tests (`slow_commit`, `invalid_request` after a commit).

### Agent side, step 3 slice 2: the reconcile loop (ADR-016, ADR-017) (2026-11-12)

**What shipped:**
- **The reconcile loop** as a second task in the worker process (`agent/worker/reconcile.py`,
  `agent/db/reconcile.py`): each pass alerts on stale uncertain runs, reads `GET /config` afresh
  and feeds the gate, claims one `NEEDS_RECONCILIATION` run whose quiet period has passed
  (`last_attempt_at + greatest(in_flight_bound, max_client_timeout + budget_now) + margin`), and
  looks it up under the usual lease, heartbeat and fencing. Found → `confirmed` (two or more →
  alert); not found → `rejected`/`ESCALATED` with the "couldn't confirm" reply, unless the kill
  switch is on (then no decision, a cooldown, no streak). It never sends.
- **Failures:** every release has the cooldown; the cooldown and the streak are now separate
  choices (`release_claim(count_streak=…)`). refund-api's failures close the gate and raise one
  alert for all runs (401/403 page); the run's own (a mismatched row, no payload, an unexpected
  error) count toward the streak and page at the limit, status untouched.
- **ADR-017 (locked 2026-11-13 after two reviews):** a repeated refund-api-type failure counts only if refund-api answered
  another run's lookup since, not merely a config read. `classify_lookup` now says whose fault a
  failure is (`provider` / `run`).
- `RECONCILER_QUIET_MARGIN_SECONDS`, `RECONCILER_STALE_ALERT_SECONDS`; the readiness log names
  both loops; the heartbeat renews reconciler claims too.
- **e2e:** `slow_commit` (the agent gives up, refund-api commits 1 s later, the reconciler waits
  and confirms one refund) and `invalid_request` after a commit (confirmed, never resent, never
  rejected), via a new one-shot refund-api test-build mode `invalid_request_after_commit`.
- 48 new reconciler tests; 17 planted regressions, each caught for its own reason (among them: the
  last send's bound instead of the largest, the kill switch ignored, answered lookups not counted
  as evidence, the quiet condition dropped, which made the e2e run end `rejected`).

**What broke:**
- **ADR-016's own rule, before any code ran.** It counted a repeated refund-api-type failure as
  the run's once a config read had succeeded in between. refund-api's `GET /config` is a static
  response that never touches its database, so a database outage would have looked like every
  uncertain run being broken at once and paged for each. Changed the evidence to a successful
  lookup for another run (ADR-017).
- A reconciler claim would have been lost at its first heartbeat: `renew_lease` only renewed
  `APPROVED`/`EXECUTING`. A slow-lookup test with a short lease proves the fix (its control: the
  old status set loses the claim).
- The second loop's own config reads consumed answers that the send-loop tests had scripted one
  by one; those tests now run with the reconcile loop swapped for an idle stand-in, and a separate
  test runs both loops together.
- The e2e profile gave refund-api the same 2 s budget as the agent's timeout, so a slow commit
  could never land after the agent gave up; the budget is now 5 s.

**What I learned:** check what a piece of evidence actually exercises: a health signal from one
endpoint says nothing about another endpoint's dependencies.

**What's next:** ADR-017's stale-unknown page (S3/S4: pages past a second threshold, skipped
only for runs whose latest "not found" the kill switch held). Then ADR-018: review found the send
path is blind to the same outage (the gate trusts the static config read), so a refund-api
database outage would escalate every refund approved during it; a circuit breaker judged on real
request outcomes, before the worker is deployed.

### Agent side, step 3 slice 3: the stale-unknown page (ADR-017 S3/S4) (2026-11-13)

**What shipped:**
- ADR-017 locked after a second review, with a pointer line in ADR-016 Part 3; reconcile loop
  pushed (`6f0aec5`).
- **A stale-unknown page** past `RECONCILER_STALE_PAGE_SECONDS` (refused at startup unless it
  exceeds the alert threshold), rate-limited per run: the route to a person for a run whose
  lookups fail alone and so never build a streak.
- **Scoped to what the kill switch actually held:** a run whose latest lookup was a "not found"
  the switch held skips its own page (its alert still fires), and those runs page once together;
  every other uncertain run keeps its page while the switch is on.
- 7 tests (lone run pages once per interval; switch on with one held and one self-failing run;
  any other outcome clears the hold; a restart forgets holds; the threshold guard), with controls
  for suppressing every run and for paging every held run; 6 plants caught.

**What broke:** the first version of the review's S4 would have silenced every uncertain run
while the switch was on, including one stuck for its own reason: its only route to a person. And
one of my own tests expected the page in the same pass that cleared the hold; the scan runs
before the claim, so it arrives one pass later, which the test now states.

**What I learned:** a suppression rule needs the same precision as the thing it suppresses: the
switch holds "not found" decisions, not runs.

**What's next:** ADR-018, a provider circuit breaker on real request outcomes, before the worker
is deployed.

### ADR-018 locked: provider breakers (2026-11-13)

**What shipped:** ADR-018 after two review passes, with pointer lines in ADR-012, 014, 016 and
017. Two in-process provider breakers judged on real request outcomes, one per path (sends gate
the send loop, lookups the reconcile loop); probes from the run that costs least to lose; every
smaller rule tested by a planned negative control.

**What broke (in the design):** the first draft shared one breaker, so a successful lookup could
reopen the send path during a write outage, and counting sends as evidence reintroduced ADR-017's
bug. The second pass found that a failed probe on a fresh run starts its send window, that read
outages would page once per uncertain run through the stale page, and that "breaker" already meant
ADR-009's policy breaker.

**What I learned:** a health check proves only what it touches; and a probe is never free, so
choose who pays for it.

**What's next:** the code, starting with refund-api's per-action fault mode for the e2e test.

### Agent side, step 4: provider breakers (ADR-018) (2026-11-13)

**What shipped:**
- `agent/worker/provider_health.py`: two in-process provider breakers, write (fed by sends, gates
  the send loop) and read (fed by lookups, gates the reconcile loop), starting half-open, with a
  doubling, capped open interval and a page for long outages. `core.refund_outcome.send_health` /
  `lookup_health` say what a response means for health (no answer, 5xx, an undocumented status or
  a non-JSON body fail; 409/429/401/403 are neutral), with the documented status sets pinned to the
  OpenAPI contract by a test.
- The send loop claims only while its breaker allows; a half-open probe claim orders by cost (a run
  doomed by its window, then an approval, then fewest attempts); a run between attempts is released
  with the cooldown and no streak when the breaker opens; a probe claim that sends nothing frees
  the slot. The reconcile loop's read breaker replaces `gate.lookup_failed()` (which also stopped
  the send loop), holds ADR-017's evidence count, and suppresses per-run stale pages during a read
  outage except for runs that last failed on their own.
- `PROVIDER_BREAKER_*` settings with startup guards (threshold below the attempt count; the
  breaker's page before the stale page).
- refund-api's test build: a per-action `fail_all` fault (sends via the existing hook; lookups via
  an innermost middleware, after auth).
- e2e: an outage on the test's actions pauses sending (runs stay `EXECUTING`, attempts left) and
  every refund completes after the plan is lifted, none escalated.
- Tests: 38 unit, 22 integration, 1 e2e; 21 plants caught.

**What broke:**
- Two plants slipped through at first. The doomed-first test passed without the probe ordering,
  because the doomed run was also the only `EXECUTING` one, which ADR-014's order picks anyway; it
  now has a healthy `EXECUTING` run approved earlier. The half-open-start test passed with breakers
  starting closed, because its helper passed the state explicitly; a test now builds them through
  the production factory.
- Two e2e tests changed meaning: a fresh worker's first send is a probe, so a timeout there pauses
  sending instead of retrying. They now warm the worker with one refund, which is the running
  worker their paths belong to, and say so.
- A plant of mine was itself broken (an emptied function body is a syntax error, exit code 4);
  re-planted as `pass`, it was caught.

**What I learned:** a negative control has to make the two orders disagree, or it proves nothing;
and a test helper that passes the default explicitly can't catch a changed default.

**What's next:** the agent half of the seed and the order-lookup tool.

### Agent side, step 5: the agent's demo seed (ADR-019) (2026-11-13)

**What shipped:**
- ADR-019 (both choices by the user, in chat): an existing order whose facts (owner, amount,
  currency) differ from the manifest refuses the seed; its fulfillment state is updated; delivery
  dates are relative (`delivered_days_ago`) on the database clock.
- `python -m agent.seed <manifest>` as the owner role, one transaction; the demo manifest switched
  to relative dates; README deploy steps run both seeds.
- 22 tests (manifest validation, first run, re-run, refused facts, exit codes); 7 plants caught.

**What broke:** nothing in the code, but the manifest's absolute dates would have put every demo
order outside the 30-day refund window by early December, silently. And one of my plants was
invalid (it compared a 1-tuple with a 3-tuple, so everything mismatched and the test "passed" for
the wrong reason); re-planted on both sides, it was caught.

**What I learned:** fixture data rots like code; dates in a demo dataset need to be relative to
when it's loaded.

**What's next:** the order-lookup tool (its strict-xfail placeholder is waiting).

**Review pass (ADR-019 locked):** orders with an unfinished run are left alone and reported; the
seed refuses without `DEMO_SEED_ENABLED=true` (exit 2); unlisted orders are never touched; the demo
manifest gained an order 45 days out so R08's refusal is still demonstrable, and a test keeps demo
dates away from the window's edge; the seed takes customer locks first (ADR-005). 7 new tests, 5
plants caught. Flagged: refund-api's seed has no demo guard yet (it also sets the production key
floor, so it needs a split).


### refund-api: the key-retention floor split from the demo seed (ADR-021) (2026-11-14)

**What shipped:**
- ADR-021 (option chosen by the user, in chat): the key-retention floor has its own owner-role
  command, `python -m refund_api.retention_floor`, run on every deployment; `refund_api.seed`
  writes only the demo charges and refuses (exit 2) unless `DEMO_SEED_ENABLED=true`, like the
  agent's seed. Rejected: one command with the flag gating only the charges, an Alembic hook, and
  setting the floor at app startup (the runtime role must not write it).
- The e2e harness deploys a demo: the floor command, then the seed with the flag from its
  profile. README deploy steps, `.env.example` and migration 0004's refusal hint name the new
  command.
- 12 new test cases (9 for the floor command, 3 for the seed); 7 plants caught, two of them
  end-to-end (no floor: refund-api answers 500 with "key retention floor is not set" and the run
  never completes; no flag: the harness's setup stops at the seed's exit 2).

**What broke:** my first round of plants all "passed" because I sourced `.env` from a wrong path,
so the integration tests were skipped, not run. Re-run with the right path, every plant was caught.
The plant script now prints pytest's summary line, so a skip can't pass for a result.

**What I learned:** a command that mixes a production setting with fixture data can't be safely
gated by either; and a negative control that ends in "exit 0" proves nothing until you've seen
the tests actually run.

**What's next:** the order-lookup tool (ADR-020, locked). Flagged: refund-api's `seconds()` config
helper raises `OverflowError` on `inf` or `1e30` instead of a clean refusal.

**Review pass (ADR-021 locked):** a skipped floor was safe but found late, at the first refund, as
a provider-breaker page that didn't name the skipped step. refund-api now checks at startup,
through a read-only `SECURITY DEFINER` function (migration 0005, executable only by the app group),
and refuses to start with a message naming the command; an unreachable database stops the start
too, with its own message (user's choice). Both builds, tested against real uvicorn because
in-process clients don't run the lifespan; 8 tests, 5 plants caught. I'd first written that a
down database gives 503; checking the code showed it's a 500 (only pool timeouts map to 503).


### Agent: the order-lookup tool (ADR-020) (2026-11-14)

**What shipped:**
- `agent/tools/`: the minimal tool contract (a strict arguments model that is the schema, a
  code-built `ToolContext`, canonical JSON results, `LostTurn`) and `lookup_order`. Ownership is
  in the answering query's `WHERE`; a miss returns the constant not-found, then a best-effort
  existence probe in its own transaction writes `cross_user_order_lookup` to `agent.security`
  only for a foreign order; a database error is one generic "temporarily unavailable"; a found
  order is recorded in `run_order_lookups` (R02), fenced by the turn lease.
- `agent/core/currency.py`: `charged_display` from an ISO 4217 exponent table (USD, JPY); an
  unknown currency is the generic error, never a guess. `agent/db/order_lookup.py`: the queries.
- The strict-xfail placeholder is replaced by the real byte-for-byte test. 44 new test cases (22
  integration as the runtime role, 22 unit); 14 plants caught: every negative control in
  ADR-020's table (the exponent one at unit and integration level) plus three of my own (the
  fence's `FOR SHARE`, the id-format check, the UTC date).
- Agent: 791 tests, no xfail. refund_api 255, e2e 16.

**What broke:** one negative control was invalid at first: the planted leak left a transaction
open, so the test failed on that instead of on the leak. The plant script checks the failure
reason, not just the exit code, and flagged it; fixed, the plant failed on the planted message.
mypy (strict re-exports) caught a test reaching a function through the tool module.

**What I learned:** a lease check followed by a write is still a race; the check has to lock the
row it reads (`FOR SHARE`, which a takeover's `UPDATE` must wait for) for the length of that one
transaction. And input that can't be valid shouldn't reach the database at all: a null byte would
have turned "not found" into "temporarily unavailable", a different answer.

**What's next:** the LLM loop (ADR-001/002/006), which consumes this contract.


### Agent: the turn designed (ADR-022, locked) (2026-11-14)

**What shipped:** documents only. The loop's open decisions were split (the user's choice): the
turn first, then the provider adapter (ADR-023), streaming with the chat API, KB search on its
own. ADR-022 locks the turn: the `events` table as the conversation log, tool results never
replayed; a provider failure gives "try again" and leaves the run waiting; untrusted text in a
per-turn nonce fence; the turn's steps, budgets and timeouts, every turn transaction with
statement and lock timeouts. Two outside reviews. Pointer lines in ADR-001/002/008/009; README
row; EVAL_PLAN category 3 gains the reply-laundering case.

**What broke:** nothing ran, but review found a demo-visible trap: after a reply naming an order,
"yes, refund it" starts a new run, the model proposes without a fresh lookup, and R02 (a hard
rule) escalates an innocent customer. The turn now bounces that proposal back to the model first.
Review also caught a summary written under the lease (a 409 for a quick follow-up) and a trigger
that fired on every turn of a long conversation. One review premise was wrong: no retention rule
exists for conversation data, so that gap is recorded as such.

**What I learned:** a hard rule plus a model shortcut becomes a customer-facing failure unless
the loop steers the model back before policy sees it. And "on overflow" isn't a trigger: once a
conversation is long, it's every turn.

**What's next:** ADR-023 (the provider adapter: AsyncOpenAI per ADR-005, retry under the turn's
deadline, telemetry; needs the `openai` package), then build the turn, with C01, behind the
scripted model.


### Agent: the LLM provider adapter designed (ADR-023, locked) (2026-11-14)

**What shipped:** documents only. ADR-023 locks the adapter: `AsyncOpenAI` against Groq's and
Gemini's OpenAI-compatible endpoints with the SDK's retries off; every call streams and is
assembled, with an optional text callback; retries only inside the caller's deadline; errors
classified by cause; an optional fallback chosen per turn; an append-only `llm_calls` row per
call with list-price cost. One outside review. Pointer lines in ADR-005/006/022; README row.

**What broke:** nothing ran, but review found two real holes. Retrying a stream after text had
reached the callback would show the customer the answer twice; now there's no retry after
delivery, and a callback error is never a provider failure. And every 400 paged, so a customer's
message rejected by the provider's filter would have woken someone; input-driven 400s are now
their own errors. Also: 429s are classified per provider (unknown means rate limit), and a cost
total over a partially billed attempt is labeled a lower bound.

**What I learned:** map errors by cause, not status code, and ask of every page "can a customer
trigger this?". Two features that are each correct (streaming, retrying) can be wrong together.
Project 1 had less to reuse than ADR-005 assumed.

**What's next:** build the adapter, the turn and C01 behind the scripted model (the `openai`
package needs approval), then capture real Groq and Gemini fixtures (keys added by the user).


### Agent: the model adapter and the scripted model (ADR-023, slice 1) (2026-11-14)

**What shipped:**
- `agent/llm/`: the normalized types and the closed error set; the OpenAI-compatible adapter
  (always streams and assembles, SDK retries off, retries only inside the deadline, no retry
  once text reached the callback, the callback isolated, usage completeness `full`/`partial`/
  `none`, an output character cap); error classification by cause (unknown 429 → rate limit,
  unknown 400 → request bug); settings from the environment with the scripted-model gate.
- `ScriptedLLM` in `testing/`: strict playback, structural matchers, recorded prompts, chunked
  text, uncounted usage never zero.
- ADR-006's contract test: the minimum scenario set through both, identical responses and
  error types. 57 new tests; 15 plants caught. Agent 848, refund_api 255, e2e 16.
- `openai` 3.51 added (approved). `.env.example` gains the `LLM_*` settings.

**What broke:** `openai` 3.x moved to `httpx2`, which `respx` (ADR-006's named mock) can't
intercept. Found by checking the SDK before writing tests; the user chose `httpx2.MockTransport`
over pinning `openai<3`; ADR-006 got a pointer line. Two plants failed earlier than predicted (the
fake clock ran past the deadline; the strict mock refused the SDK's retry); both were the planted
bug, and I recorded the real reason instead of loosening the checks.

**What I learned:** check what a dependency actually does before testing against it; make mocks
strict enough that "the mock wasn't hit" can't pass.

**What's next:** slice 2: `llm_calls` and the price table, the fallback cooldown; then the turn
and C01. Real Groq and Gemini fixtures need the user's keys.


### Agent: call records, prices and the provider pool (ADR-023, slice 2) (2026-11-14)

**What shipped:**
- `llm_calls` (migration 0004): one append-only row per model call, success or failure, with
  attempts, tokens, usage completeness, characters, timings and list-price cost; CHECKs so a row
  can't claim a cost without tokens and a price version; no foreign keys, so telemetry never
  waits behind a worker's row lock.
- `RecordingModel` (one record per call, steps numbered, best-effort, not lease-fenced) and
  `DbCallRecorder` (its own short, timeout-bounded transaction).
- The price table: exact decimal strings, each with its source and checked date; unpriced is
  NULL, never zero. Shipped empty: no model chosen yet, and no invented prices.
- `ProviderPool`: one provider per turn, a cooldown and an alert on a spent quota or failed
  auth, the fallback from the next turn, fail-fast with no network when nothing is usable, and a
  page on a request bug.
- 41 new tests; 11 plants caught (two of them in the migration, the dev schema rebuilt after).
  Agent 889, refund_api 255, e2e 16.

**What broke:** one constraint test row violated two CHECKs, so Postgres named a different one
than expected; each row now breaks exactly one. Two plant reasons were pytest's parenthesized
form of the same assertion, recorded as such.

**What I learned:** a foreign key on a hot telemetry table is a lock dependency on the money
path; constraints can carry the integrity instead.

**What's next:** the turn (ADR-022) and C01, behind the scripted model; then real Groq and
Gemini fixtures and prices once the models are chosen (keys added by the user).


### Agent: the turn's foundations (ADR-022, slice T1) (2026-11-15)

**What shipped:** `TurnSettings` (the 14 `TURN_*` settings, validated, cross-checked with the
model's minimum attempt); the start transaction (lease or `TurnInProgress`; a missing and a
foreign conversation both `ConversationNotFound`; oversized or empty messages refused before
anything is saved; a pending proposal answered with a status template and the lease handed
back, no model; resume or create the run); fenced finishers (`reply_and_complete`,
`reply_and_wait` with `try_again`'s cause); the turn heartbeat (`LostTurn` on a takeover,
`TurnOverrun` past the deadline plus one statement timeout); the tool connection with
session-level timeouts reset before it returns to the pool. 28 tests, 11 plants caught. Agent
917, refund_api 255, e2e 16.

**What broke:** nothing in the code. I removed a `FOR UPDATE` from the status read at turn start
before testing (a pending run may be held by a worker mid-send), and made the overrun test's
stuck work outlive its outer bound so a broken backstop fails cleanly instead of racing.

**What I learned:** `SET LOCAL` can't reach a transaction someone else opens; session settings
on a pooled connection are a leak unless reset (or the connection discarded).

**What's next:** T2, C01 on customer messages at the turn's start.


### Agent: C01 on customer messages (ADR-009, turn slice T2) (2026-11-15)

**What shipped:** `agent/core/c01.py`, deterministic legal-and-abuse patterns with stable ids
over NFKC-normalized, case-folded text (straight apostrophes, collapsed spaces), returning ids
only. Wired into the turn's start transaction: in `AGENT_STEP` (new or resumed) the run escalates
with `c01_flagged_at`, a `conversation_escalation` decision row (pattern ids, never text) and a
`needs_person` reply, with no model call; after a proposal the run is flagged, its
`review_revision` bumped (A03) and the refund left alone, and the customer still gets the status
template. The core import allowlist grew by `re` and `unicodedata`. 30 tests, 6 plants caught.
Agent 947, refund_api 255, e2e 16.

**What broke:** ruff caught literal look-alike characters (curly apostrophes, full-width letters)
where I meant `\u` escapes, in the code and the tests; now escapes, so the file says what it
tests. The shared turn settings moved to a helper module instead of one test importing another.

**What I learned:** document a classifier's blind spots as tests (negation over-escalates,
obfuscation slips through), so a later "improvement" that changes them is visible.

**What's next:** T3, the read-only loop.


### Agent: the read-only loop (ADR-022, turn slice T3) (2026-11-15)

**What shipped:** the first complete turn, message in and answer out, on the scripted model.
`turn/prompt.py` (a versioned system prompt; the window from message events only, trimmed
oldest-first to its character budget with `window_trimmed`; the per-turn nonce fence that
escapes planted markers; earlier template replies as notices); `turn/tools.py` (read tools with
schemas from their argument models); `turn/loop.py` (read calls in order on connections with the
turn's timeouts, results only as tool messages; malformed responses answered with error results
and nothing run; the call and malformed budgets; model errors mapped per ADR-023 S1a);
`turn/run.py` (`run_turn`: start, loop under the lease with the provider pool and call
recording, fenced finish, the alert after repeated provider failures). 27 tests, 9 plants
caught. Agent 974, refund_api 255, e2e 16.

**What broke:** nothing in the code. One plant failed one assertion earlier than I'd named (the
window's character budget before the dropped-message check); recorded as the real reason.

**What I learned:** "all or nothing" for a malformed response is easy to state and easy to get
wrong: the valid calls in a broken response must not run either.

**What's next:** T4, the write tools and ADR-009's proposal path.


### Agent: the write tools and ADR-009's proposal path (turn slice T4) (2026-11-15)

**What shipped:** the model can now propose. `turn/tools.py` gains `propose_refund(order_id,
amount_minor, currency)` and `escalate(reason_code)` (a closed set; neither has an identity
field). `turn/loop.py` accepts a write call only alone in its response (mixed, nothing runs) and
bounces a proposal on an order this run never looked up back to the model as a malformed
response ("yes, refund it" now looks the order up, then proposes). `turn/propose.py` +
`db/proposal.py` are ADR-009's proposal path: one transaction locks the customer, fences on the
turn, reads every per-customer count under the lock, evaluates, writes the decision row, and
moves the run: `APPROVED` with idempotency key, canonical payload and review hash (ADR-004
step 1), `AWAIT_APPROVAL` with a review deadline and a `pending_review` event, or `ESCALATED`;
the breaker trips with its own decision row and an alert; template replies only, no model call
after a proposal. Lock timeouts are retried, then "try again" with cause `lock`; anything that
can't be evaluated fails closed in a separate turn-fenced transaction. `core/review_hash.py`
(A01, with hand-computed vectors). New settings: `POLICY_BREAKER_WINDOW_SECONDS`,
`TURN_PROPOSAL_LOCK_ATTEMPTS`, `REVIEW_DEADLINE_SECONDS`. `make_approved_run` now builds its run
with the production `approval_values`. 35 tests, 21 plants caught. Agent 1009, refund_api 255,
e2e 16.

**What broke:** a lock-timeout test failed before the model was called: inserting a run checks
its customer foreign key with `FOR KEY SHARE`, which our `FOR UPDATE` customer lock blocks. A
real behavior (a proposal or pre-send briefly blocks new runs for that customer), recorded in
KNOWN_TRADEOFFS with `FOR NO KEY UPDATE` as the pending fix. The R13 total's test was vacuous:
its planted bug passed, because the fixture helper never set `approved_at` on the refused row.
Two other "uncaught" plants were caught for the right reason; my expected text didn't match
pytest's truncated output.

**What I learned:** a design sentence ("gather inputs before the lock") can be read two ways, and
the literal reading reintroduces the race the lock exists for; and a fixture helper that quietly
skips a field can make a test unable to fail.

**What's next:** a decision on the customer lock's mode, then T5, memory (past-ticket digest and
rolling summary).


### Agent: the customer lock's mode (ADR-024) (2026-11-16)

**What shipped:** ADR-024, locked: the per-customer lock is `FOR NO KEY UPDATE`, superseding
ADR-005's `FOR UPDATE` (pointer lines in ADR-005 and ADR-009). Measured first on our Postgres:
under `FOR UPDATE` an insert referencing the customer was blocked; under `FOR NO KEY UPDATE` it
proceeds and a second limit check still waits. Code: one statement, `presend.customer_lock`,
used by the pre-send check, the proposal path and the seed. `test_customer_lock.py`: inserts and
a whole new turn go through while a limit check holds the customer, a second limit check still
waits, the seed uses the shared statement and doesn't wait on a foreign-key check. 5 tests, 7
plants caught (including T4's concurrent-proposal control re-run under the new mode). Agent
1014, refund_api 255, e2e 16.

**What broke:** nothing in the code. The machine had restarted and our Postgres container was
stopped, so the first test run errored on connection; `docker compose up -d --wait postgres`
brought it back on 5433.

**What I learned:** "nobody updates this row" isn't "nothing locks this row": foreign-key checks
lock the parent. Writing the ADR also showed R18 is approximate (turn-start escalations don't
take the customer lock), now in KNOWN_TRADEOFFS.

**What's next:** T5, memory (past-ticket digest and rolling summary).


### Agent: memory (ADR-002, ADR-022 S6, turn slice T5) (2026-11-16)

**What shipped:** the turn's memory. Migration 0005 adds `conversation_summaries` (append-only,
the last event each summary covers, its prompt version and model). The window is now the last N
message events plus every one no summary covers yet; dropping an uncovered one for the character
budget logs `summary_gap`. When the messages beyond N reach `SUMMARY_TRIGGER_MESSAGES`, the turn
starts a background summary after its reply and lease release, with its own deadline; readers
take the summary covering the latest event, and an insert is refused under a newer one. The
past-ticket digest is the user's last K other runs from database fields (confirmed refunds as
issued, unknown as pending, nothing for rejected or none), fenced like the summary. A summary
failure of any kind is logged as `summary_failed` and the old summary stays. 21 tests, 12 plants
caught (one on the migration's append-only grant, with the schema rebuilt from base after).
Agent 1035, refund_api 255, e2e 16.

**What broke:** a flaky test, about one run in eight: it expected one stored summary, but the
follow-up message it sent had read its window before the first summary was stored and started
its own. ADR-022 allows that (a wasted call, never a wrong summary); the test now asserts the
summary readers use covers the newest cut-off, and passed 15 runs in a row. A fence assertion
could match the system prompt's own mention of the marker; it now matches real nonce-paired
fences only.

**What I learned:** when a concurrent test flakes, check what the design actually promises
before touching the code; the test may be claiming more.

**What's next:** T6, one end-to-end path from a customer message through the proposal and the
worker to an issued refund.


### End to end: a customer message to an issued refund (turn slice T6) (2026-11-17)

**What shipped:** `python -m agent_testing.scripted_turn`, a second agent test entry point
beside `make_approved_run` (ADR-015 Part 3's pattern; pointer line in ADR-015): it creates a
customer, an order and a conversation, then runs one production turn on a scripted model (look
the order up, propose the refund) with real policy, decision log, approval and call records.
`tests/e2e/test_conversation.py`: the turn's APPROVED run is sent by the crash-test worker to the
real refund-api and ends COMPLETED/confirmed, exactly one refund, the customer told "processing"
then "refund issued"; the control, the same script on a shipped order, is escalated before any
worker starts and refund-api never sees it. The e2e profile gained the turn's settings. 2 e2e
tests and 4 agent tests; 4 plants caught. Agent 1039, refund_api 255, e2e 18.

**What broke:** the control first passed with proposal-time policy planted broken: the worker's
pre-send re-check refused the refund on the same rule (R09), so no money moved. That's ADR-009's
two layers working, but the control couldn't tell which layer refused; it now checks the run is
escalated right after the turn, and the plant fails it.

**What I learned:** defense in depth can hide a broken layer from an end-state test; a control has
to pin down the layer it's about.

**What's next:** choosing the real models (Groq/Gemini) and capturing fixtures, then the chat API.


### Decision: real models on free tiers (ADR-025) (2026-11-17)

**What shipped:** ADR-025, locked after two outside reviews (pointer lines in ADR-023 and
EVAL_PLAN). Free tiers only, one account per provider (Groq, Gemini, Cloudflare; Ollama
local-only); credentials and an ordered model list per purpose from the environment; production
refuses a model not verified for its purpose with the current prompt fingerprint and a context
that fits; tool calling proven per provider and model; record-and-replay evals with deterministic
eval prompts; a daily eval budget that keeps a demo reserve; a merge gate for prompt changes;
synthetic data only on the demo. Query routing is next as ADR-026. No code yet.

**What broke:** nothing built. The reviews caught that replay keys would never have matched (a
random nonce and relative dates in every prompt), that evals could eat the demo's quota, and that
"verified" would go stale after a prompt change.

**What I learned:** pooling free accounts is a terms problem, not an engineering one; the
engineering answer is fewer live calls, not more accounts. And record-and-replay only works if
everything that reaches a prompt is deterministic.

**What's next:** one key per provider (the developer adds them), one measuring smoke run, then
ADR-025's code.


### Agent: providers, model lists and startup checks (ADR-025 step 1) (2026-11-19)

**What shipped:** one credential set per provider (Groq, Gemini, Cloudflare with its account id,
OpenRouter, Cerebras, SambaNova, Ollama), replacing `LLM_PROVIDER`/`LLM_MODEL`/`LLM_API_KEY`/
`LLM_FALLBACK_*`; `LLM_TURN_MODELS` and `LLM_SUMMARY_MODELS` as ordered `provider:model` lists
(each provider once per list; model names keep their own colons). All seven hosts are in the code
table: Cloudflare's URL from a template and an id-format check, Ollama's loopback-only URL rebuilt
from its parsed parts and refused in production. The provider pool takes an ordered list per
purpose with one cooldown map shared across purposes, so the summary no longer rides on the
turn's model. Startup checks (`agent.turn.model_checks`): a turn model needs a tool-call proof
(`tool_call_proofs.json`, bound to captured fixtures by a unit test); `verified_models.json` must
hold the model for its purpose and the current prompt fingerprint (refused in production, warned
elsewhere); the model's context must hold the purpose's largest prompt plus output. Both files
are empty until the first capture and eval. Agent 1144 (was 1039), refund_api 255, e2e 18; 25
plants caught.

**Decisions inside the locked ADR (user's choices):** the fingerprint covers every fixed
model-facing text, rendered through the real functions with fixed inputs, not just the system
prompt and tool schemas; S12's bound adds the turn's own earlier responses, which the ADR's
formula left out; S4's proof lives in a committed registry the service can read, since `tests/`
doesn't ship; the same provider twice in a list is refused.

**What broke:** with today's defaults the worst-case turn is 17,118 tokens, not the ADR's ~11K:
its formula skipped five earlier responses (5,120 tokens). Still fits 24K models. Two parts have
no bound at all yet (several lookups in one response; the summary's input while summaries keep
failing), recorded as tradeoffs for a follow-up ADR. `run.py` lost its `Purpose` import in the
rename; unit tests didn't import it, ruff caught it before the integration suite did. Two
fingerprint tests first failed because they patched a module whose constant another module had
imported by name; the fingerprint now reads constants through their owning modules. Two plants
were themselves wrong (one made the test die with `KeyError`, one expected the wrong Hypothesis
wording) and were fixed before counting.

**What I learned:** a "largest prompt" formula written in prose can miss the part that grows
fastest; making a test assemble the real worst-case prompt and compare is what exposed it. And
a fingerprint is only as good as its coverage, so render through the code instead of listing
strings.

**What's next:** the measuring smoke run per provider: capture tool-call and error fixtures, fill
`tool_call_proofs.json`, measure tokens per conversation from `llm_calls`.


### Agent: data terms, captured fixtures and the first measuring smoke run (ADR-025 S4/S5/S11) (2026-11-19)

**What shipped:** data terms checked and recorded for Groq, Cloudflare, SambaNova, Cerebras and
OpenRouter (runbook). `python -m agent_testing.capture` saves one real response per request as a
fixture (one attempt, keys and masked key echoes redacted, only rate-limit headers kept);
`python -m agent_testing.smoke_turn` runs the production turn on a real model for three synthetic
conversations plus a summary and reports tokens from `llm_calls`. Tool calling proven for six
models (Groq gpt-oss-120b, both Cloudflare candidates, three Gemini models), now in
`tool_call_proofs.json`; auth fixtures for all six providers; fixture-driven tests replay every
captured response through the adapter. Measured numbers are in the runbook: a first call is
~650–1,000 input tokens, not the 2,500 ADR-025 estimated.

**What broke (all found by the real run, none by the mocks):** (1) Cloudflare and Gemini failed
every turn's second call with 400, paged as `InvalidRequest`: Cloudflare refuses `null` content
on an assistant tool-call message (now `""`); Gemini wants each call's `thought_signature`
echoed back (not fixed yet: a decision). (2) Cloudflare's Llama streams number-like pieces as
JSON numbers: the assembler crashed with `TypeError` (outside the adapter's closed error set),
and before that a falsy `0` was silently dropped, turning "0.05" into ".05". Converting numbers
back isn't exact either (" 9999" arrives as 9999, its space gone), so such a stream is now
refused as a provider error. (3) Gemini answers a bad key with 400 (now `AuthFailed` by a
marker); Cerebras answers 402 `payment_required` (now `QuotaExhausted`, never a page).
(4) SambaNova's key is rejected for chat; the "verified" check two days earlier hit a `/models`
endpoint that answers without any key. (5) One SambaNova error body echoed a masked fragment of
the key; capture now redacts it, with a test and a fixture-wide check. 12 plants caught.

**What I learned:** a mock only proves what you already believed about a provider; the second
call of a conversation is where compatibility layers differ. A check that can't fail (a public
endpoint) is worse than no check, because it gets written down as proof. And when a provider
hands back a lossy value, refusing beats repairing.

**What's next:** the Gemini thought-signature decision; a SambaNova key; the record-and-replay
runner; adversarial cases from this run (an injection-framed message got a full refund within
policy).

**Same day, Gemini (user's choice: carry provider data):** `ToolCall` gained an optional
`extra_content` (JSON text): the assembler keeps what a provider attaches to a tool call, and the
next assistant message sends it back unchanged. Gemini's second calls went from 400 to 200, and
both Flash-Lite models now complete turns (~1.6K input tokens per two-call turn; 3.1 refused the
injection, 3.5 proposed the full charge like Groq). 2 plants caught.

**Same day, providers:** Cerebras dropped (host table, credential variable, fixtures). SambaNova
accepts the replaced key, but every model now answers `402 PAYMENT_METHOD_REQUIRED` (balance 0),
so it's out under S5 too; its captured 402s now pin the 402 → `QuotaExhausted` rule.

**Same day, OpenRouter:** with the account's free-model training toggle off, seven `:free` models
offered tools; four made the tool call (ERNIE 5.5 Flash, Cohere North Mini Code, dots-3, Flint
v1.0) and three answered 429 from OpenRouter's shared upstream pool (pinned as rate limits). All
four completed smoke-run turns; three refused the injection, Cohere proposed the full charge.
One summary failed with a relayed upstream 400 that passed on a re-run (recorded, not
classified without a captured body).


### Decision and slice: the eval runner (ADR-026) and its clock seams (2026-11-19)

**What shipped:** ADR-026, locked after one outside review (pointer lines in ADR-025, which now
calls routing ADR-027, and EVAL_PLAN). Slice 1, the seams: `agent.db.clock` (a `Clock` protocol;
production's `DatabaseClock` reads `None` and every turn-path write is `COALESCE(:at, now())`, so
production stamps stay each transaction's own `now()`), an id source for run ids and an injected
fence nonce for the turn and the summary. Every turn-path write takes the clock once per
transaction: 9 former SQL `now()` writes and 7 inserts that relied on column defaults (runs,
events, decisions, summaries, order lookups); the policy's "now" comes from the clock when it
gives one. `TurnDeps`/`SummaryDeps` carry production defaults; the eval side is
`agent_testing.evals.clock` (a logical clock at `ANCHOR = 2026-01-15T12:00Z`, seeded ids and
nonces). Tests: a scan that allows SQL clock reads only in the lease statements and the policy's
production fallback; production stamps equal the lease's transaction time; the refund window
follows the anchor (29 days in, 31 out) while the real clock refuses both; every stamp of an
approval, a review, a C01 escalation, an escalate call and a summary sits just after the anchor;
the digest shows the seeded ticket id and the anchor's date, fenced with the seeded nonce. Agent
1211 (was 1199); 10 plants caught.

**What broke:** fixed seeded ids collided with earlier runs' rows in the shared test database
(`pk_runs`), exactly what ADR-026 Part 1 predicted; slice-1 tests take a per-execution seed, and
"the same case twice gives identical prompts" waits for the runner's fresh database per case. A
replaced docstring lost its closing quotes; a test stub didn't accept the new keyword. The scan's
first version flagged Python variables named `now`; it now looks for SQL only. ADR-026 S2a's
invariant bound (anchor + 1 day) is too tight for `review_deadline` (a stamp plus 24 hours); the
runner's invariant must allow deadline columns their configured offset.

**What I learned:** making the new parameter required, not defaulted, let the type checker list
every caller that needed it; the integration test then showed the one it couldn't (a stub typed
`Any`).

**What's next:** slice 2, the recording layer (S2's wrapper, replay/record/live, the anchor
guard, the real-date leak check), then the runner with fresh databases per case.

**Slice 2, the recording layer (same day):** `agent_testing.evals.recording`: `EvalModel` wraps a
case's model and keys every call by ADR-025's SHA-256 (provider, model, prompt fingerprint,
messages, tools, temperature, output cap, run index); `replay` returns the stored response or
fails with `MissingRecording` naming the key and never calls the model, `record` writes one JSON
file per key, `live` stores nothing. Each recording stores the anchor and replay refuses another
(`AnchorMismatch`, naming both); every mode refuses a prompt containing the real date
(`RealDateLeak`) before any call; a call that raises is never recorded; evals are refused in
production. 20 tests, 7 plants caught (two first named the wrong failure text and were
re-aimed: a strict model being called is the right failure for "replay fell back to live").
Agent 1231.

**Same day, SambaNova dropped:** removed from the host table, `CREDENTIAL_VARS` and
`.env.example`, its runbook rows and its fixtures, as Cerebras was. Its two 402
`PAYMENT_METHOD_REQUIRED` bodies were the only captured 402s, so they moved to
`tests/unit/llm_fixtures_retired/` and replay under SambaNova's former host-table entry (put back
for the test only); a test also checks a retired provider stays out of the table. Planting
402 → `RateLimited` fails exactly those two tests and nothing else in the unit suite, so they are
the pin. Agent 1229 (5 SambaNova cases removed, 3 added); refund_api 255; e2e 18.

**What broke:** my first plan was to capture a fresh 402 from OpenRouter by calling a paid model
on the $0 free-tier account, expecting a refusal. OpenRouter served it (Amazon Bedrock) and
billed $0.0001233, so the balance went negative. Checked by API before (credits 0, usage 0) and
after (usage 0.0001233). Recorded in the runbook and KNOWN_TRADEOFFS: nothing in code keeps
OpenRouter models on `:free`.

**What I learned:** "no payment method" isn't "can't be billed": test a spending assumption
with a read (the price list, the account's balance policy), never with the call itself.

**Same day, escalation reasons:** migration 0006 adds `runs.escalation_reason`, so ADR-026's
`escalated` judge can read why a run went to a person from the database. Every path that
escalates now stores a reason from `agent.core.escalations`: `escalate_tool:<code>`,
`malformed_budget`, `step_budget`, `c01`, `policy:<rules>`, `fail_closed:<rules that raised>`,
`presend_policy:<rules>`, `presend_fail_closed:<rules>`, `release_streak`, `refund_rejected`,
`not_confirmed`; earlier escalated runs become `unrecorded`. Two CHECKs: an iff between ESCALATED
and having a reason, and the reason's shape (the migration copies the pattern; a unit test keeps
them equal). 7 plants caught. Agent 1265, refund_api 255, e2e 18.

**What broke:** my first pair of CHECKs (ESCALATED needs a reason; a reason needs `escalated_at`)
together implied ADR-008's `escalated_flagged`, and the parity test's negative control caught it:
dropping that check no longer changed anything. One iff on the status alone doesn't overlap.
Editing the uncommitted migration after applying it broke its own downgrade (the old constraint
names); repaired by hand on the dev database and the round trip re-checked. Fail-closed rule ids
carry a `FAIL_CLOSED:` prefix, so a reason names only the rules that raised.

**ADR-028 locked (2026-11-20) after two outside reviews:** a context bound that holds. Measured
first: a 9,543-character malformed response becomes a 415,128-character correction (each call's
error repeats a model-written tool name), so the echo is quadratic; free captures showed text of
4-byte characters costs ~0.87–0.88 tokens per byte on Groq, Gemini, Cloudflare and ERNIE, ten
times the 3-characters-per-token estimate, and all four providers accept a mid-conversation
system note. Chosen: one token per UTF-8 byte as a ceiling, checked per model by a fixture, with
byte caps on text that crosses between models; a per-turn read budget that withdraws the read
tools when spent (its own outcome, not malformed); one fixed-size note instead of the echo;
summary chunks with a per-conversation content-failure count; a window capped in messages;
`ContextTooLong` after this is a bound violation that alerts; retired setting names refused.

**What broke:** Cloudflare's gpt-oss-20b answered the note with its call inside the reasoning
channel and nothing else (pinned); OpenRouter's shared free pool refused the full ceiling
capture (429), so ERNIE was measured on a smaller payload.


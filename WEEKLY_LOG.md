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

**What's next:**
- Walking skeleton: state machine + `runs` table + one read tool + idempotent mock refund
  API + tracing, then the crash test.

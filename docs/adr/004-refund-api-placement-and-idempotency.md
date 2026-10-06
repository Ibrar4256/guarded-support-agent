# ADR-004: Refund API Placement, Idempotency Protocol and Failure Injection

**Status:** Accepted (2026-10-05). Revised the same day after outside review: policy
re-check timing, retention rationale, worker lease and fencing, retryable 4xx,
canonical payloads, `CRASH_POINT` guard. Revised 2026-10-06 (ADR-005 review): capacity is
counted by `refund_outcome`, not run status; escalation is a flag, not an outcome; workers
claim `APPROVED` and `EXECUTING` runs; escalations append a customer-facing event. Revised again 2026-10-06 (freeze review):
`unknown` is counted outside the rolling window; reconciliation waits for a quiet period
before setting `rejected`; only business-error 4xx set `rejected`; the claim query has a
status check; the heartbeat runs as a separate task. Final pass: `last_attempt_at` set at
the `EXECUTING` move, retry after "not found" also waits for the quiet period, `slow_commit`
mode, the atomic processing deadline.

**Locked 2026-10-06.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

## Context
ADR-001 says exactly-once refunds come from the downstream API deduplicating on an
idempotency key, and the crash test must prove it. The dangerous window is **after the
refund API commits and before the agent records the result**. That window only exists
if the refund API and the agent are separate systems that fail independently. So the
mock has to behave enough like a real payment provider (Stripe, Adyen) for the test to
mean anything.

Constraints: free-tier hosting for the live demo, deterministic CI with no network or
secrets, and reviews that can wait for hours (ADR-003) before a refund executes.

## Options Considered

### Option B: Separate refund-api service with its own database, called over HTTP with an Idempotency-Key header — CHOSEN
- How it works: A small FastAPI service with its **own Postgres database** (not just a
  separate schema) and its own credentials, running as a separate container in
  docker-compose. The agent reaches it only over HTTP, at a configured URL, through a
  `RefundClient` interface.
- Pros: The same failure shape as a real provider: separate process, separate storage,
  outcomes you can't be sure of. The crash test can kill only the agent worker while
  refund-api keeps running. Built-in failure injection (below) makes it a real test
  fixture. Adding Stripe later is a new adapter, not a redesign.
- Cons: One more service in compose, CI and deploy. A few milliseconds per call (which
  doesn't matter).
- Cost/latency/complexity profile: $0 locally. Low-to-medium complexity (about 150–250
  lines plus tests).

### Option A: In-process module sharing the agent's database — rejected
- How it works: The agent calls `refund_service.issue_refund()` as a function, writing to
  the same database.
- Pros: Zero infrastructure and the fastest to build.
- Cons: The refund row and the run update can share one database transaction, which
  gives exactly-once for free. No real provider allows that. Killing the process kills
  the "provider" too, so the dangerous window can't happen.
- Why we didn't use it here: The crash test would pass without proving anything.
- When it WOULD be the better choice: When refunds really are internal ledger entries in
  your own database (store credit, wallet balances). Then a single transaction is the
  correct design, not a shortcut.

### Option C: Same app, separate router, called over loopback HTTP — rejected
- How it works: The mock is mounted at `/mock/refunds` in the agent's FastAPI process and
  called over HTTP.
- Pros: Real HTTP and headers, with one deploy.
- Cons: It shares the agent's process lifetime, so you can't cleanly produce "the provider
  committed, the caller died". A shared process also makes it tempting to share a database
  session.
- Why we didn't use it here: It can't produce the failure the crash test exists for.
- When it WOULD be the better choice: Contract tests of the HTTP client (headers, status
  handling) when a separate service isn't available.

### Option D: Stripe test mode — rejected as the test fixture, planned as an optional adapter
- How it works: Real refunds against real test charges, with Stripe's own
  `Idempotency-Key` behavior.
- Pros: The most credible real-world integration; the idempotency semantics aren't ours.
- Cons: It **can't inject failures** (commit and then return a 500, or time out after
  committing). CI would need network access and secrets. Stripe prunes idempotency keys
  after about 24 hours, which matters if a retry comes after a long outage (not after long
  reviews, since the key is first sent after approval). It needs a Stripe account
  and test charges created first.
- Why we didn't use it here: A fixture that can't fail on demand can't test failure
  handling.
- When it WOULD be the better choice: One recorded demo run after the skeleton works,
  through a `StripeRefundClient` adapter behind the same interface. Never as the CI
  fixture.

### Option E: Generic HTTP mock (WireMock / Prism from an OpenAPI spec) — rejected
- How it works: Stubs generated from a spec or recorded scenarios.
- Pros: No mock code to write.
- Cons: Mostly stateless. Real deduplication needs stored keys and responses, and the
  failure modes are awkward to script.
- Why we didn't use it here: The wrong tool for a stateful protocol.
- When it WOULD be the better choice: Stateless contract mocks of read-only third-party
  APIs.

## Decision
B, with the idempotency protocol and failure injection below.

### Idempotency protocol (agent side)
1. **The key is created at proposal time and saved before any call.** When `POLICY_CHECK`
   accepts a proposal (or a reviewer approves it), the key and the full refund payload
   are written to the run row in the same transaction. A key created at call time would
   be lost in a crash, and the retry would become a new refund.
2. **Every refund request carries a reference to the action that caused it**
   (`metadata.action_id`). This lets reconciliation ask "did *this action* already
   refund?" rather than just "did this order get a refund?". An order can legitimately
   have more than one partial refund.
3. **Interpreting responses:**
   | Response | Meaning | Agent behavior |
   |---|---|---|
   | 2xx | Done (fresh or replayed) | Record the result; continue |
   | **Timeout / connection reset** | **Unknown**, *not* failed | Retry with the **same key** |
   | **409** (same key in flight) | Wait | Retry with the same key after backoff |
   | 422 (same key, different payload) | Bug in our code | Stop; escalate; alert |
   | Other 5xx | Possibly committed | Retry with the same key |
   | 429 | Rate limited, not committed | Retry with the same key after backoff; honor `Retry-After`, capped at the remaining retry window |
   | 408 | Unknown | Same as a timeout: retry with the same key |
   | 400 **business error with a provider error code** (e.g. `refund_exceeds_charge`); 422 is reserved for key/payload mismatch | Rejected, not committed | `refund_outcome = rejected`; escalate |
   | 401 / 403 / 404 and other auth or routing errors | Says nothing about whether this action committed | Keep `unknown`; alert; go to reconciliation |
4. **Retries are bounded.** After N attempts with exponential backoff, the run moves to
   `NEEDS_RECONCILIATION` and escalates to a human. It is **never** marked "failed" while
   the outcome is unknown. **Escalation is a flag plus an event (`escalated_at`, an
   `escalated` row in `events`); it never changes `refund_outcome`.** A run that escalates
   with an unknown refund keeps counting against limits until reconciliation resolves
   it.
5. **Reconcile before retrying an old key.** If more time has passed since the key's
   **first send** than the key-retention window (for example after a long outage), the
   agent first calls `GET /refunds?order_id=…&action_id=…`. If a refund exists, the agent
   records it. If not, it waits until the quiet period (below) has passed and then
   retries with the same key. A key that old may have been pruned, which makes the retry
   effectively a new refund. If a hung request commits after the "not found" read, the
   retry would then refund twice.
   **Reconciliation may set `rejected` only after a quiet period.** "No refund found" is a
   point-in-time read, and a request that hung (`timeout_after_commit`) may still be
   processing and commit a moment later. So `unknown → rejected` through reconciliation is
   allowed only when `now() − last_attempt_at` > client timeout + the provider's maximum
   processing time. Every send updates `last_attempt_at`. The mock enforces that maximum
   with a server-side request deadline: a request not committed by the deadline is aborted
   and never commits. That makes the bound true by construction, not just assumed. (A real
   provider's processing bound is an assumption; this is noted for the Stripe adapter.)
6. **Policy is checked once, before the first attempt.** The ADR-003 re-check and the
   `APPROVED → EXECUTING` transition happen in **one transaction**, before any HTTP call.
   That same transaction writes `first_send_started_at` and sets `last_attempt_at` to the
   same value, so an `unknown` action that crashed before its first HTTP call still has a
   quiet-period anchor. Step 5 computes key age from that
   value, and a resumed worker reads it to tell "never sent" (run still `APPROVED`) from
   "maybe sent" (`EXECUTING` with `first_send_started_at` set). If the check rejects, the
   transaction moves the run to `ESCALATE` instead, with nothing sent. That's the only path
   where a check after approval can stop the action. The check **never** runs again on
   crash-resume or retry. Once a key may have been sent, the run is committed to
   finishing that action, and re-checking can only block a refund that already happened.
   (Because unknown refunds count as issued, a resumed $40 refund on a $50 per-order limit
   would look like $80 and be rejected.) As a backstop, the capacity calculation always
   leaves out the action being evaluated (its own `action_id`).
7. **Claiming a run: lease plus fencing.** A worker claims a run with compare-and-set
   (`UPDATE runs SET worker_id=…, lease_expires_at=now()+:lease,
   claim_version=claim_version+1 WHERE id=… AND status IN ('APPROVED','EXECUTING') AND
   (worker_id IS NULL OR lease_expires_at < now())`) and extends the lease with a
   heartbeat. The status check means a `COMPLETED`, `ESCALATE` or other finished run can
   never be claimed, and every move to a finished status clears `worker_id` and
   `lease_expires_at`. **Lease times always use the database clock (`now()`), never the
   worker's**, so clock drift between workers can't affect who owns a run. Workers claim **`APPROVED` and `EXECUTING`** runs whose lease is free or
   expired. `APPROVED` runs wait for the policy check and the move to `EXECUTING`, and
   lock-timeout retries (ADR-005) also happen in `APPROVED`. On a lock timeout the worker
   **keeps its claim** and keeps heartbeating while it backs off. Every write
   to the run row includes `AND claim_version = <the version this worker claimed>`. A
   worker that stalls past its lease and then wakes up can't overwrite a run another
   worker has since claimed. refund-api's 409 and key deduplication protect the refund
   (a stale worker's HTTP retry just replays); the fencing check protects the run row.
   - **The heartbeat runs as its own asyncio task**, separate from the coroutine making
     the HTTP call, so a slow call or a lock wait doesn't delay renewal. The lease is
     sized as a multiple of the heartbeat interval (at least 3×, enforced at startup).
   - **Liveness, not just "the process is alive":** the heartbeat task stops renewing if
     the current step runs past its maximum duration (client timeout + max backoff sleep
     + `lock_timeout`, with margin). Otherwise a coroutine stuck forever would keep its
     claim forever. When it stops renewing, the lease expires and another worker takes
     over safely through fencing.
   - **If a heartbeat write affects 0 rows, the worker has lost its claim.** It stops
     immediately, including mid-retry: it cancels the in-flight attempt, sends nothing
     further, and writes nothing further. Anything already sent is covered by key
     deduplication.
8. **Canonical payloads.** Amounts are integer minor units (cents) everywhere. The payload
   bound to a key is the canonical JSON (sorted keys, no floats) of `order_id`,
   `amount_minor`, `currency` and `action_id`, and nothing else: no timestamps, request IDs
   or retry counters, which would make an honest retry look like a different payload.
   This way `20.0` vs `20.00` can't cause a false 422.

### refund-api contract (mock side)
- **Key retention covers the time from first send to the last possible retry.** The
  provider first sees a key when the refund call is made, which is after approval, so
  review time doesn't use up retention. Retention must exceed the **maximum retry window
  plus the maximum recovery delay** (worst-case downtime before a worker resumes and
  retries). All three are config values, and the service refuses to start otherwise. This is a
  **config guard, not a guarantee**: the maximum recovery delay is an assumption.
  Review time would matter only if keys were ever registered with the provider ahead of
  time, which we don't do. Delays beyond the assumed maximum recovery delay are covered by
  reconciliation by `action_id` (protocol step 5). This is set explicitly because Stripe
  prunes keys after ~24 hours.
- The key, the stored response and the refund row are written in one transaction.
- Business rejections return **400 with an error code** and store no key, so they're never
  replayed. 422 means only "same key, different payload".
- Each request has a server-side processing deadline (config). If it isn't committed by
  then, it aborts and never commits, which is the bound the reconciliation quiet period
  relies on. **Making it atomic:** the transaction sets `SET LOCAL statement_timeout` to
  the remaining budget, and immediately before `COMMIT` it checks
  `clock_timestamp() < :deadline` and rolls back if not. ★ It must be
  `clock_timestamp()`, not `now()`: in Postgres, `now()` is frozen at transaction start,
  so a `now()` check would always pass. The tiny gap between that check and the commit is
  covered by a safety margin in the quiet period (config).
- Same key, different payload → **422**. Same key while the first request is still being
  processed → **409**.
- `GET /refunds?order_id=&action_id=` is the reconciliation endpoint.

### Failure injection (mock side, test configuration only)
| Mode | Behavior | What it proves |
|---|---|---|
| `normal` | Commit, 201 | Happy path |
| `fail_before_commit` | 500, nothing written | Retry is safe |
| `fail_after_commit` | Commit, then 500 | Replay returns the original; no second refund |
| `timeout_after_commit` | Commit, then hang past the client timeout | A timeout is treated as unknown, not failed |
| `slow_commit` | Delay, then commit within the processing deadline | A reconciliation read during the delay finds nothing; the quiet period must stop it from setting `rejected` |
| `dedup_disabled` | Ignore keys | Negative control: tests must detect a double refund |

Each failure mode has its own integration test (see `docs/EVAL_PLAN.md`, refund-api
failure modes), `timeout_after_commit` in particular.

### Crash tests (agent side, debug-only `CRASH_POINT`, `os._exit(1)`)
`CRASH_POINT` is honored only when `DEBUG_FAULT_INJECTION=true`, and the service refuses
to start if either one is set while `APP_ENV=production`, so it can never fire in the
live demo.

- `before_refund_call`: the key is saved, nothing has been sent → on resume there is
  **exactly one** refund.
- `after_refund_response`: refund-api committed, the agent hasn't recorded it → on resume,
  replay with the same key gives **exactly one** refund.
- `after_refund_response` with a **tight limit**: a $40 refund on a $50 per-order limit,
  crash, resume → the refund is recorded and **not** blocked by the policy check
  (protocol step 6).
- **Stale worker:** worker A claims a run and stalls past its lease; worker B claims and
  finishes it; A wakes up. Assert **both**: (1) A's run-row write affects 0 rows (fencing
  rejected it) and the run shows B's result, and (2) refund-api holds **exactly one**
  refund for the action, even though A's HTTP retry may reach it.
- **Rejected at execution:** the policy changes between approval and execution, so the
  check rejects → the run is `ESCALATE`, `first_send_started_at` is null,
  `refund_outcome` is `none`, refund-api holds zero refunds, **and an `escalated` event
  for the customer is appended to `events` in the same transaction** (ADR-003), so the
  chat doesn't stay on "pending review" forever.
- **Escalation keeps capacity reserved:** bounded retries are exhausted with an unknown
  outcome → `NEEDS_RECONCILIATION` plus an escalation flag; the action still counts
  against the customer's limit; a second refund that would exceed the limit is rejected.
- **A business-error 4xx frees capacity:** refund-api returns 400 with an error code for a
  refund it never committed → `refund_outcome = rejected`, and the capacity is available
  again. A 401/403/404 after an earlier unknown attempt keeps `unknown` and alerts.
- **Quiet period:** `slow_commit`; reconciliation runs before
  the quiet period → the outcome stays `unknown`; after it → the refund is found and marked
  `confirmed`, never `rejected`.
- **Old unknown still counts:** an `unknown` action older than the per-customer window
  still blocks a refund that would exceed the limit.
- **Lost claim mid-retry:** force a heartbeat to affect 0 rows → the worker sends no
  further attempts and writes nothing more.
- **No claiming finished runs:** a claim attempt on a `COMPLETED` or `ESCALATE` run affects
  0 rows.
- Both crash tests run again against `dedup_disabled`; the after-response case must find
  **two**. The tight-limit variant also finds **two** under `dedup_disabled`: the policy
  check is skipped on resume by design (step 6), so a second refund there is the expected
  result of the negative control, **not** a policy bug.
- **Test data rule:** if refund-api ever validates that refunds can't exceed the charge
  (as real providers do), the order amount in negative-control tests must be at least
  twice the refund amount. Otherwise the over-refund check rejects the second refund and
  the negative control can never fail.

### Source of truth for refund history
- The **agent's database** holds refund history for the ADR-002 digest and for
  `POLICY_CHECK`. Each action's outcome becomes `unknown` at the move to `EXECUTING` and
  changes again when it's resolved (by a response or by reconciliation against
  refund-api).
- **Capacity is counted by refund outcome, not by run status.** Each action has
  `refund_outcome` ∈ {`none`, `unknown`, `confirmed`, `rejected`}:
  | Outcome | Set when | Counts against limits? |
  |---|---|---|
  | `none` | Proposed or approved; nothing sent | No |
  | `unknown` | In the same transaction as `APPROVED → EXECUTING` (it might be sent from then on); stays `unknown` after a timeout, 5xx, 408 or 422 | **Yes** |
  | `confirmed` | 2xx (fresh or replayed), or reconciliation finds the refund | **Yes** |
  | `rejected` | A business-error 4xx with a provider error code (the mock stores responses only for commits, so this is never a replay), or reconciliation finds no refund **after the quiet period** (step 5). Auth and routing errors (401/403/404) never set it | No |
- The capacity query is `refund_outcome IN ('unknown', 'confirmed')`, excluding the
  action being evaluated. Status changes (`ESCALATE`, `NEEDS_RECONCILIATION`, review
  flags) never change this count; only an outcome change does.
- **Time window:** per-order limits cover the order's lifetime. Per-customer limits apply
  a rolling window (a config value, for example 30 days, measured from
  `first_send_started_at`) **to `confirmed` actions only. Every `unknown` action counts
  no matter how old it is.** Otherwise a forgotten `unknown` would age out of the window
  and free capacity.
- **Stale-unknown alert:** a `NEEDS_RECONCILIATION` run older than a threshold (config)
  raises an alert, so uncertainty can't sit unresolved.
- Uncertainty never frees up capacity: only `unknown → rejected` (a business-error 4xx,
  or reconciliation after the quiet period) can release it. `confirmed` is final.

## Consequences
- **Adapter from day one.** `RefundClient` is an interface; `MockRefundClient` is the only
  implementation now. A later `StripeRefundClient` is a small, contained change.
- **Live demo compromise.** If the chosen host's free tier can't run two services, the
  demo runs agent and refund-api as **two processes in one container**. They still have
  separate databases and credentials, and HTTP over a configured URL, so no code relies on
  the shortcut. A container restart kills both, so the crash window can't be shown on the
  live demo; the compose-based CI crash tests cover it. This is checked against free-tier
  limits in the deployment ADR, and two services are preferred if available. Recorded in
  `KNOWN_TRADEOFFS.md`.
- **CI services:** CI runs two Postgres databases and two app containers.
- **ADR-003 is narrowed:** the execution-time policy re-check happens exactly once, before
  the first attempt (protocol step 6).
- **Revisit if:** we integrate a real provider (then key retention and reconciliation
  follow the provider's rules), or if refund-api grows features unrelated to testing
  failures.

## Interview-ready summary
"I put the mock refund API in its own service with its own database, because exactly-once
across a network boundary is the whole problem. If the refund and my run update shared a
transaction, the crash test would pass without proving anything. The protocol: the
idempotency key is created when the action is approved and saved before any call. A
timeout means unknown, not failed, so I retry with the same key. A 409 means wait. A 422
means my code reused a key for a different payload. After bounded retries the run goes to
reconciliation, never to 'failed'. Policy is re-checked once, before the first send, never
on resume, because re-checking an in-flight refund can only block one that already
happened. Runs are claimed with a lease plus a fencing version, so a stalled worker can't
overwrite a run another worker has taken over. The mock can commit and then return a 500, or commit
and then hang, which Stripe test mode can't do, so Stripe is a later adapter, not the
fixture. Crash tests cover both sides of the call, and a dedup-disabled negative control
proves the test can fail. An in-process ledger would be the right design if refunds were
internal store credit, because then a single transaction is correct."

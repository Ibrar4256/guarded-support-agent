# ADR-010: refund-api Implementation Decisions

**Status:** Accepted (2026-10-09). Revised 2026-10-14 after outside review: `invalid_request`
no longer sets `rejected` (agent-side amendment), the processing deadline counts time before
the transaction starts, the charge row is locked before the over-refund check, service
authentication (Part 6), a production seed path, fault-plan semantics that can't pass
without the fault applying, a `SECURITY DEFINER` prune function with an owner-set retention
floor, and contract rows. Final pass: the receipt middleware wraps auth, and the
duplicate-refund accounting gap is recorded.

**Locked 2026-10-14.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Precedence:** for **refund-api behavior and the agent's handling of refund-api
responses**, ADR-010 > ADR-009 > ADR-008 > ADR-001–007. It changes nothing in ADR-009's
policy rules. The amendments to earlier ADRs are listed in "Amends" below; their locked text
gets only pointer lines.

## Context
ADR-004 fixes refund-api's protocol: one transaction for key, stored response and refund;
422 only for "same key, different payload"; 409 while the same key is in flight; 400 with
a business-error code for rejections; key retention; a processing deadline; failure modes
in test configuration only. Starting the implementation surfaced gaps it doesn't settle:

1. **FastAPI returns 422 for every malformed request**, which collides with ADR-004's single
   meaning for 422. And because validation runs **before** the handler, a validation error
   says nothing about whether an earlier attempt with the same key committed.
2. **What refund-api knows about charges**, which decides the business-error enum, how the
   "a business-error 4xx frees capacity" crash test produces a 400, and how the live demo
   gets charges at all.
3. **How a test selects a failure mode**, such that a test can't pass without the fault
   being applied. ADR-007 settles where fault code lives, not how it's chosen.
4. **How "same key in flight" is detected** without breaking the one-transaction rule.
5. **What key retention does at the mock**, beyond ADR-004's startup config guard.
6. **Who may call refund-api.** ADR-004 maps 401/403 responses, but no ADR defines what
   produces them. Anyone who can reach the URL could issue refunds.

Constraints: deterministic CI against real Postgres (ADR-005), test code absent from the
production image (ADR-007), tests that can run in parallel, one developer, and a free-tier
live demo (ADR-004 Consequences).

## Amends (agent side)
1. **ADR-004 step 3 table:** a 400 with code **`invalid_request`** means "unknown", like
   422: the run moves to `NEEDS_RECONCILIATION` with `escalated_at` set and an alert, and
   `refund_outcome` stays `unknown`. Only the **other** business-error codes set
   `rejected`. This applies **uniformly, including to what looks like a first attempt**:
   after a crash and resume, the worker can't reliably know whether an earlier attempt
   reached refund-api.
   - *Why:* validation runs before the key lookup. Attempt 1 commits but times out on the
     agent's side; a client bug makes the retry malformed; refund-api returns
     `invalid_request` without ever looking at the key. Treating that as `rejected` would
     free capacity and tell the customer no refund was made when one was. Business errors
     don't have this problem: they're checked after the lookup, so a committed key replays
     its stored response instead.
2. **ADR-007 contract rule:** "only a recognized business-error code can set `rejected`"
   becomes "only a recognized business-error code **other than `invalid_request`** can set
   `rejected`".
3. **ADR-008 transitions:** `EXECUTING → NEEDS_RECONCILIATION` also covers
   `invalid_request`. **The reconciler's lookup:**
   - Any lookup failure (timeout, 5xx, 400, 401/403/404, an unparseable body) keeps the
     outcome `unknown`, raises an alert and retries later. **An error is never read as
     "not found".** Only a 2xx with an empty list is "not found".
   - The lookup returns a list. **Two or more refunds for one `action_id`** (possible after
     a pruned key, or a bug) → `confirmed`, plus a duplicate-refund alert for a person to
     handle. The capacity count records the action's amount once; the alert is what
     catches the money that moved twice.

## Options Considered

### Part 1 — Request-validation errors

#### Option A: 400 `invalid_request`, treated by the agent as unknown — CHOSEN
- How it works: A `RequestValidationError` handler returns 400 with code
  `invalid_request` and the offending field locations only (no values, no internals). It
  covers the body, headers (a missing or malformed `Idempotency-Key`), query parameters on
  `GET /refunds`, and unparseable JSON. FastAPI's auto-generated 422 response is removed
  from every route in the OpenAPI spec, so the only declared 422 is the key mismatch. The
  agent treats `invalid_request` as unknown (Amends 1).
- Pros: 422 keeps one meaning ("my code reused a key with a different payload"), which
  tells an on-call engineer which bug to look for. No money bug: a malformed retry of a
  committed refund ends `confirmed` through the reconciler, never `rejected`.
- Cons: A malformed first attempt holds capacity until the reconciler's quiet period
  passes, then resolves to `rejected`. That's only a delay, and it only happens when the
  agent has a bug, which the alert reports.
- Cost/latency/complexity profile: One exception handler, one spec post-processing step,
  contract tests for each validation location.

#### Option B: Look the key up before validating the body (server-side fix) — rejected
- How it works: Parse the `Idempotency-Key` first, take the lock and look the key up; replay
  or return 422 if it exists, and validate the body only for new keys. Then a 400 could
  honestly mean "nothing committed under this key".
- Pros: Fixes the hazard at the source, at refund-api.
- Cons: Bypasses FastAPI's validation with hand-written raw-body handling, and it touches
  the database before it knows the request is well formed. Most importantly, the agent's
  safety would depend on **this mock's** validation order. A real provider adapter
  (Stripe, ADR-004 Option D) gives no such guarantee.
- Why we didn't use it here: The agent rule has to hold for any provider; the server-side
  order is an implementation detail it can't rely on.
- When it WOULD be the better choice: A provider you own that has many clients, none of
  which you control, where you want 400 to carry a "nothing committed" guarantee.

#### Option C: A separate status for validation errors (400 without a code, or 418) — rejected
- How it works: Validation errors get their own status or a code-less 400.
- Pros: Separates "our client is broken" from business rejections at the status level.
- Cons: Another response class for the agent and the contract test. Under ADR-007 it would
  map to `unknown` anyway, so it buys nothing over Option A's code.
- Why we didn't use it here: Option A already separates the cases with a code and an
  alert.
- When it WOULD be the better choice: Third-party clients that need a different support
  path for malformed requests.

#### Option D: Leave FastAPI's default 422 — rejected
- How it works: Keep the framework default.
- Pros: No code.
- Cons: A schema bug looks exactly like a key mismatch, and the two need different fixes.
- Why we didn't use it here: It breaks ADR-004's single meaning for 422.
- When it WOULD be the better choice: An API where 422 has no protocol meaning.

### Part 2 — What refund-api knows about charges

#### Option A: A `charges` table, locked before the over-refund check, with a seed path for production — CHOSEN
- How it works:
  - `charges(order_id PRIMARY KEY, amount_minor, currency)`: **one charge per order** in
    v1.
  - After the key lookup finds nothing, the transaction takes `SELECT … FROM charges WHERE
    order_id = :o FOR UPDATE` and only then sums the order's committed refunds. Two
    different keys for one order (two actions, or `dedup_disabled`) can't both pass the
    check. The lock order is: advisory lock (try-only, never waits), then the charge row,
    so it can't deadlock.
  - Business-error codes: `refund_exceeds_charge` (counts **all** committed refund rows),
    `charge_not_found`, `currency_mismatch`, and `invalid_request` (Part 1).
  - **Tests** create charges through an endpoint in `refund_api_testing`, which doesn't
    exist in the production image. The test helper defaults the charge to at least twice
    the refund (ADR-004's test-data rule), so negative controls can always fail.
  - **The live demo** gets charges from a **seed command run as the owner role**, like
    migrations. One deploy-time **manifest** (orders with their charged amounts) is the
    single input. The agent's seed command writes orders, and refund-api's writes
    charges, each through its own service as its own owner role, so the two can't drift.
- Pros: Real business-error codes, the way real providers behave, and a second line of
  defense behind ADR-009's R05. No over-refund race. The demo can actually issue refunds.
- Cons: Test and demo data span two databases. Two seed commands to maintain.
- Cost/latency/complexity profile: One row lock and one aggregate per new refund; one
  small seed command per service.

#### Option B: No charges; codes only `invalid_request` and `unsupported_currency` — rejected
- How it works: refund-api validates only the request's shape and currency.
- Pros: Simpler; no cross-database data.
- Cons: refund-api accepts any amount. The capacity-freeing test would need a currency
  trick instead of a realistic rejection.
- Why we didn't use it here: It weakens the test that business errors free capacity.
- When it WOULD be the better choice: A pure idempotency fixture where business validation
  is entirely the caller's job.

#### Option C: Both services read a shared fixture file at runtime — rejected
- How it works: Each service loads the same order and charge file on startup.
- Pros: One source of data.
- Cons: A file both services read **at runtime** is shared state, close to the shared
  code ADR-007 forbids, and tests can't create the exact charge they need.
- Why we didn't use it here: It blurs the service boundary. (Option A's manifest is
  different: a deploy-time input, applied once by each service's own command.)
- When it WOULD be the better choice: A fixed demo dataset with no per-test setup.

### Part 3 — How a test selects a failure mode

#### Option A: A fault plan per `action_id`, consumed per mode and asserted as applied — CHOSEN
- How it works:
  - A test-control endpoint, mounted only when `DEBUG_FAULT_INJECTION=true` (and the
    service refuses to start in production with that flag set, per the ADR-006
    production-gate test), registers a plan for one `action_id`.
  - **Consumption is per mode.** The crash and failure modes (`fail_before_commit`,
    `fail_after_commit`, `timeout_after_commit`, `slow_commit`) are **one-shot**: used by
    the first matching request. **`dedup_disabled` stays in effect** for its `action_id`
    until removed, because a one-shot version would let the retry deduplicate normally,
    and the negative control could then never fail.
  - **No TTL.** A plan lasts until it's consumed or removed. Each test uses unique
    `action_id`s, and its fixture teardown removes **only its own** `action_id`s. A
    global reset exists only for between-session cleanup. A teardown that reset globally
    would delete a parallel test's plan and cause flaky failures.
  - **Every fault test asserts the plan was applied**, by reading the fault log for its
    `action_id`. A plan that never fired (wrong ID, wrong process) fails the test instead
    of passing in `normal` mode. Every applied fault is logged with its `action_id` and
    mode.
  - **The retention override is part of the plan** (Part 5), because parallel tests share
    one process and so one process-wide retention value.
  - **One worker process.** Plans live in memory, so the test compose file pins a single
    uvicorn worker. The app can't reliably detect its worker count, so it doesn't try; with
    more than one worker, the applied-check fails loudly.
- Pros: Modes change without restarts, tests run in parallel, and a fault that didn't
  happen can't produce a pass.
- Cons: Tied to a single worker process in test configuration.
- Cost/latency/complexity profile: A dictionary lookup per request; about 80 lines in
  `refund_api_testing`.

#### Option B: Plans in a test-only database table — rejected
- How it works: The test-control endpoint writes plans to a table; any worker reads them.
- Pros: Removes the single-worker constraint.
- Cons: A test-only table would have to appear in the production schema and migrations,
  or need a separate test-only migration path.
- Why we didn't use it here: The single-worker pin plus the applied-check covers the risk
  without putting test structures into the production database.
- When it WOULD be the better choice: Tests that need several refund-api workers, such as
  load or concurrency tests across processes.

#### Option C: One global mode per process, set by environment variable — rejected
- How it works: `FAULT_MODE=fail_after_commit` at startup.
- Pros: The simplest; no endpoint.
- Cons: Every mode change needs a restart, tests can't run in parallel, and one wrong mode
  affects every request.
- Why we didn't use it here: Too slow and too coarse for a dozen mode-specific tests.
- When it WOULD be the better choice: A long-running chaos environment that holds one mode
  for hours.

#### Option D: A per-request header naming the fault — rejected
- How it works: The client sends `X-Fault-Mode`.
- Pros: Maximum flexibility.
- Cons: The caller chooses its own fault, which is the wrong trust direction, and it's one
  misconfigured gate away from production misuse.
- Why we didn't use it here: Fault selection belongs to the test harness, not the client
  under test.
- When it WOULD be the better choice: Client-library tests against a disposable local stub.

### Part 4 — Detecting "same key in flight" (409)

#### Option A: Non-blocking, transaction-scoped advisory lock on the key — CHOSEN
- How it works: `pg_try_advisory_xact_lock` on a 64-bit hash of the key, taken first,
  before any fault-injection delay. If it isn't acquired → **409** immediately. If it is:
  prune the key if expired (Part 5), look it up (replay, 422, or continue), lock the charge
  row (Part 2), then create the refund and store the key and response in the same
  transaction. Lock → prune → lookup means two requests can't race on the delete.
  - The 409 body has its **own schema with no business-error code**, so the agent can't
    mistake it for a rejection.
  - The lock-key function is an **injectable seam** (ADR-007 hooks style), so a test can
    force two different keys onto one lock value and check the spurious 409 succeeds on
    retry. Production code has no branch for it.
  - Transaction-scoped advisory locks also work behind PgBouncer in transaction mode (the
    ADR-005 deployment concern).
- Pros: Keeps ADR-004's single transaction. A crash releases the lock, so nothing is left
  stuck. 409 is immediate, not a wait.
- Cons: Hash collisions cause a spurious 409, which the agent retries.
- Cost/latency/complexity profile: One function call per request.

#### Option B: Commit a "processing" key row first (Stripe-style) — rejected
- How it works: Insert the key as `processing` in its own transaction, do the work, then
  update the row.
- Pros: In-flight state is visible in the table; what large providers do.
- Cons: Two transactions break ADR-004's rule, and a crash between them leaves a stuck row
  that needs a recovery job.
- Why we didn't use it here: More moving parts, and it contradicts a locked ADR.
- When it WOULD be the better choice: Long-running work that can't fit in one database
  transaction, such as calls to a downstream bank.

#### Option C: Rely on the primary key's uniqueness — rejected
- How it works: Both requests insert the key; Postgres makes the second wait.
- Pros: No extra mechanism.
- Cons: The second request **blocks** instead of getting 409; a `lock_timeout` workaround
  makes the result timing-dependent.
- Why we didn't use it here: ADR-004 promises 409.
- When it WOULD be the better choice: Rare duplicates where waiting is acceptable.

### Part 5 — Key retention at the mock

#### Option A: Prune on access, through a `SECURITY DEFINER` function — CHOSEN
- How it works: Inside the request transaction, after the lock, refund-api calls
  `prune_expired_key(key, retention)`. The function deletes that one key row only if it's
  older than `retention`, which must be positive. **Refund rows are never deleted**, so
  reconciliation by `action_id` still finds them.
  - The runtime role has **no `DELETE`** on `idempotency_keys`. The function is owned by
    the owner role, pins `search_path`, has **`EXECUTE` revoked from `PUBLIC`** (Postgres
    grants it to `PUBLIC` by default) and granted only to `refund_api_app`.
  - **Why a function:** bug containment, not defense against a compromised role. A
    compromised runtime role can already insert refund rows, which is worse than deleting
    keys. The function stops application code from deleting a key by any other route.
    It bounds the damage to one key at a time, never refunds, and **never a retention
    below the floor**.
  - **Retention floor:** the function deletes only if the key is older than
    `greatest(retention, floor)`, where the floor is read from a one-row table
    (`key_retention_policy`) owned by the owner role, with **no privileges for the app
    group**. The owner role sets it at deploy time (the production retention guarded by
    ADR-004's startup check); the test environment sets a small value. A code bug passing a
    tiny retention can't shorten retention below the floor. ADR-004's startup guard covers
    bad config, the floor covers bad code. A custom setting (`ALTER DATABASE … SET
    refund_api.min_key_retention`) was considered and rejected: any role can `SET` a custom
    setting in its own session, so the runtime role could lower it.
  - Retention comes from config, or from the test's fault plan (Part 3), with real short
    durations rather than a fake clock.
- Pros: Expiry is deterministic and testable without a job. It makes the hazard real: an
  expired key re-sent under `normal` mode creates a **second refund**. A refund-api test
  records that hazard, and an agent-side test shows the worker never re-sends past
  retention (ADR-008).
- Cons: Keys never touched again are never pruned, so the table grows without bound in a
  long-running deployment (`KNOWN_TRADEOFFS.md`). The function adds a migration object to
  review.
- Cost/latency/complexity profile: One function call per request.

#### Option B: Config guard only, never prune — rejected
- How it works: ADR-004's startup check and nothing else.
- Pros: No code.
- Cons: The retention-expiry path never runs in any test.
- Why we didn't use it here: Untested safety reasoning.
- When it WOULD be the better choice: A provider whose pruning you don't control (Stripe),
  where the guard is all you have.

#### Option C: A periodic prune job — rejected for now
- How it works: A background task deletes expired keys every N seconds.
- Pros: Bounds the table's size, the way a real provider does.
- Cons: Tests depend on its timing, so it would have to be disabled in tests; the live
  demo's volume is too small to need it.
- Why we didn't use it here: Option A has the same effect deterministically, and the
  table-growth problem doesn't exist at demo scale.
- When it WOULD be the better choice: Any long-running deployment with real volume, added
  alongside Option A, not instead of it.

### Part 6 — Service authentication

#### Option A: A bearer service token checked in middleware, plus a private network — CHOSEN
- How it works:
  - Every refund-api route except health checks, **including `GET /refunds`**, requires
    `Authorization: Bearer <token>`. The check runs as **middleware, before routing and
    validation**, so an unauthenticated request gets 401 (never a 400 that reveals the
    schema) and commits nothing.
  - Comparison is constant-time (`hmac.compare_digest`). refund-api accepts **two
    tokens**, current and next, so a token can be rotated without downtime. The agent
    sends one. Values live in `.env` and the host's secret store, never in code.
  - refund-api isn't reachable from the public internet: an internal network, or
    localhost when both run in one container (ADR-004's demo compromise).
  - The agent already maps 401/403 to "keep `unknown`, alert" (ADR-004 step 3), and the
    reconciler does the same on lookups (Amends 3).
- Pros: Simple, standard, testable, and it closes the "anyone with the URL can refund"
  hole. Network isolation and the token each cover the other's failure.
- Cons: A shared secret to manage and rotate. It authenticates the caller but doesn't sign
  the request body.
- Cost/latency/complexity profile: One header comparison per request.

#### Option B: Mutual TLS — rejected
- How it works: Each service presents a client certificate; refund-api verifies it.
- Pros: Strong identity, no shared secret in headers.
- Cons: Certificate issuance and rotation, and free-tier hosts generally terminate TLS at
  their own proxy, so it may not even be possible there.
- Why we didn't use it here: Operational cost out of proportion for one caller on free
  hosting.
- When it WOULD be the better choice: A service mesh or many internal services, where
  certificates are issued automatically.

#### Option C: Network isolation only — rejected
- How it works: refund-api is reachable only on a private network; no credential.
- Pros: Nothing to manage.
- Cons: One misconfigured ingress or port mapping exposes refunds to anyone.
- Why we didn't use it here: Money movement needs a second layer.
- When it WOULD be the better choice: Two processes on one host talking over a Unix socket.

#### Option D: HMAC request signing — rejected
- How it works: The agent signs method, path, body and a timestamp with a shared secret;
  refund-api verifies the signature and the timestamp window.
- Pros: Protects the body against tampering and replay by intermediaries.
- Cons: Canonicalization and clock-window handling on both sides, which are new places
  for false rejections.
- Why we didn't use it here: There are no untrusted intermediaries between the two
  services.
- When it WOULD be the better choice: Requests crossing untrusted networks, or webhooks
  sent to third parties.

## Decision
Part 1 A, Part 2 A, Part 3 A, Part 4 A, Part 5 A, Part 6 A. One theme runs through them:
every failure the protocol claims to handle must be **reachable in a deterministic test
that can't pass without the failure happening**; every response must have one meaning to
the agent; and no test convenience may exist in a production image.

### Processing deadline (ADR-004's atomic deadline, made complete)
- Middleware records **request receipt** on a **monotonic clock**. **The receipt
  middleware is the outermost layer, wrapping auth** (Part 6), so time spent
  authenticating counts against the budget.
- At transaction start: `remaining = budget − elapsed_since_receipt`. If `remaining ≤ 0`,
  abort without writing. Otherwise `deadline = clock_timestamp() + remaining`, and `SET
  LOCAL statement_timeout` to `remaining`. Immediately before `COMMIT`, check
  `clock_timestamp() < deadline` and roll back if not.
- Every comparison stays on one clock: the monotonic clock for elapsed time, the database
  clock for the commit check. Comparing the app server's wall clock with the database's
  would bring back the clock-skew problem ADR-004 avoids.
- The connection pool's acquisition timeout is shorter than the budget, so waiting for a
  connection can't consume it unnoticed.
- **Residual gap:** time between the agent sending the request and refund-api receiving
  it isn't counted. It's bounded by the load balancer's timeout and recorded in
  `KNOWN_TRADEOFFS.md`.

### Contract details
- **Canonical hash:** SHA-256 hex of `json.dumps(sort_keys=True, separators=(",", ":"),
  ensure_ascii=False)` as UTF-8, over `order_id`, `amount_minor`, `currency`, `action_id`
  (ADR-004 step 8), with **no normalization**. `contracts/hash_vectors.json` holds
  handwritten canonical strings, and each service's own tests load it (the agent's is a
  strict-xfail placeholder until `refund_client` exists). This is not ADR-009's A01
  review hash, which stays agent-only.
- **Formats in the spec, so bad input fails early:** `currency` `^[A-Z]{3}$`;
  `amount_minor` an integer from 1 to 9 223 372 036 854 775 807; `order_id` and `action_id`
  `^[A-Za-z0-9_-]{1,128}$` for now. The final `action_id` format is fixed with the
  agent's ID generator at the `refund_client` step, and the spec follows it. Request
  models use `extra="forbid"`.
- **Replay:** the stored status and the byte-identical stored body (`Text`, not `JSONB`),
  plus `Idempotent-Replayed: true`.
- **Response schemas:** 201 refund; 400 `{code, fields}` with the enum above; 401 with no
  body detail; 409 its own schema with no `code`; 422 key mismatch only.
- **In-process timeout tests are "logic only".** A real hung connection, and so real proof
  of `timeout_after_commit` handling, exists only in `tests/e2e`.

### Tests this ADR adds (each with a negative control)
| Test | Negative control |
|---|---|
| refund-api: commit a key, retry with a malformed body → 400 `invalid_request` **while the refund row exists** (records the hazard Amends 1 fixes) | — (the test *is* the hazard record; the agent-side test below shows the fix) |
| Agent (with the reconciler): attempt 1 commits, the retry gets `invalid_request` → ends `confirmed`, never `rejected` | Map `invalid_request` to `rejected` → the test fails |
| Two different keys on one charge at once, whose sum exceeds it → exactly one succeeds | Without `FOR UPDATE` on the charge row → both succeed |
| A request delayed before its transaction starts (seam) past the budget → aborted, nothing committed | Anchor the deadline at transaction start → it commits |
| The runtime role can't `DELETE` from `idempotency_keys`; another role (e.g. `agent_runtime`) can't execute `prune_expired_key`; the function never deletes a key younger than the floor, even when given a smaller retention; the runtime role can't read or change `key_retention_policy` | The owner can `DELETE` and update the floor; the runtime role can execute the function; a key older than the floor is deleted |
| Unauthenticated `POST` and `GET` → 401 and zero refund rows; a malformed unauthenticated body → 401, not 400; both rotation tokens accepted | A valid token → 201 |
| Expired key re-sent in `normal` mode → a second refund row (the hazard ADR-008 designs around) | Within retention → replay, one refund |
| Slow commit holds the lock: same key → 409; after commit, retry → replay, one refund | — |
| Forced lock-key collision → 409, then success on retry | — |
| Each fault mode asserts its plan was applied (fault log by `action_id`) | Register the plan for a different `action_id` → the applied-check fails |
| `dedup_disabled` stays on across the retry → two refunds | One-shot `dedup_disabled` → one refund, the control fails |
| Validation in headers, query parameters and invalid JSON → 400 `invalid_request`; the spec declares no 422 except the key mismatch | Re-enable FastAPI's default → 422 appears |

## Consequences
- The business-error enum is `invalid_request`, `refund_exceeds_charge`,
  `charge_not_found`, `currency_mismatch`. ADR-007's contract test requires the agent to
  list exactly this set, and to treat `invalid_request` as unknown.
- Pointer lines ("Amended by ADR-010") go into ADR-004, ADR-007, ADR-008 and ADR-009's
  precedence line when this ADR is locked.
- New secrets: `REFUND_API_TOKEN_CURRENT`, `REFUND_API_TOKEN_NEXT` (refund-api) and
  `REFUND_API_TOKEN` (agent). Placeholders go in `.env.example`; CI generates throwaway
  values.
- The test compose file runs refund-api with one worker.
- Test and demo data span two databases, kept consistent by one deploy-time manifest.
- `KNOWN_TRADEOFFS.md` gains: unbounded key-table growth without a sweep, the
  client-to-server gap in the processing deadline, and the duplicate-refund accounting gap
  (two refunds for one action count once against capacity; the alert is the control).
- **Revisit if:** a second caller appears (scope idempotency keys per caller, as Stripe
  scopes them per account); a real provider adapter lands (its validation and retention
  rules apply on that path); the demo sees real volume (add the Part 5 sweep); tests need
  several refund-api workers (Part 3 Option B).

## Interview-ready summary
"Implementing the mock refund API turned up gaps the protocol ADR didn't cover, and an
outside review caught two money bugs in my first draft. The first: FastAPI validates the
request before my handler looks up the idempotency key, so a malformed retry of a refund
that already committed came back as a 400, and the agent would have marked it rejected,
freeing capacity and telling the customer no refund happened. Now the agent treats a
validation error like an unknown outcome and lets the reconciler settle it. The second: I
anchored the processing deadline at transaction start, so time spent waiting for a
connection wasn't counted, and a request could commit after the reconciler had already
concluded 'not found'. Now the budget runs from request receipt on a monotonic clock and
is converted to a database deadline at transaction start, so every comparison stays on one
clock. In-flight detection uses a non-blocking transaction-scoped advisory lock: a
duplicate gets 409 immediately and a crash leaves nothing stuck. Fault plans are chosen per
action ID, and every fault test asserts the fault actually fired, because a test that
silently runs in normal mode passes for the wrong reason. Service calls need a bearer
token, with two accepted at once for rotation."

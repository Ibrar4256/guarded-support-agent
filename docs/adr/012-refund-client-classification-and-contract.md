# ADR-012: Refund Client — Classification, Contract, Retry Limits, Provider Config and Key Expiry

**Status:** Accepted (2026-11-12). Revised three times the same day after outside review.
- **Pass 1:** the core parses only the fields it needs (no response models); the retry limit
  holds across crashes (a per-send age check on the database clock and a persisted attempt
  count); a total per-attempt timeout; lookups matched against the full payload; the worst
  case counts every wait at the cap; "rejected is safe" written down as an assumption; the
  shared retry window enforced through an authenticated `GET /config` that also probes the
  token.
- **Pass 2:** the config probe gates only the worker's send path, never chat or API
  readiness; the hand parser made strict (duplicate keys, `NaN`/`Infinity`, exact types, a
  response-size cap); per-key expiry in refund-api so a retention change can't shorten keys
  already in flight (Part 5); which check is the real safety bound; how the config value is
  refreshed; why 409/429 count as attempts.
- **Pass 3:** a per-run window snapshot, so a *larger* window can't lengthen an old key's
  life in the agent's age check; a refreshed window below the worst case stops new claims;
  long outages sending in-flight runs to reconciliation; the floor and retention kept apart.

**Locked 2026-11-12.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Amended by [ADR-013](013-provider-config-read-before-each-send.md):** the window in Part 3's
age check is the value read from `GET /config` immediately before each send (validated and
capped), not the cached one; the cache in Part 4 only gates claiming and alerting.
`GET /config` reports durations as integer milliseconds (`max_retry_window_ms`,
`key_retention_ms`, `processing_budget_ms`).

**Precedence:** for the agent's handling of refund-api, **and for refund-api's
idempotency-key lifetime and its `GET /config` endpoint**, this ADR wins over ADR-004,
ADR-010 and ADR-011. Specifically, it:
- **narrows ADR-004:** `Retry-After` is capped at the backoff cap, not only at the remaining
  window;
- **amends ADR-010:** adds `GET /config` to refund-api's contract, and changes how
  `prune_expired_key` decides expiry (Part 5): what it promises is unchanged (never delete a
  live key, never a refund, never below the floor), but the expiry is now stamped per key;
- **amends ADR-011:** the `key_retention` hook now sets the lifetime stamped on a new key,
  instead of the retention passed to prune.

## Context
The agent talks to refund-api only through a `RefundClient` (ADR-004). Five things aren't
settled:

1. **Where the response-to-outcome mapping lives.** It's the most safety-critical code on
   the agent side: one wrong branch turns "maybe refunded" into "rejected" and frees
   capacity (ADR-010's `invalid_request` bug was exactly this).
2. **How the contract test works.** ADR-007 requires that every request the agent can send
   validates against refund-api's committed spec, every declared response parses and is
   handled, and the business-error enum is identical on both sides.
3. **The retry limits**, which must keep every send inside the window refund-api sizes its
   key retention for (ADR-004: retention > max retry window + max recovery delay), **across
   worker crashes and slow responses**, not just within one worker's loop.
4. **How the two services agree on that window, and how a wrong service token is caught**,
   without coupling customer chat to refund-api's availability.
5. **What a retention change does to keys already in flight.** ADR-010's prune compares a
   key's age with the retention configured *now*, so shrinking retention would shorten every
   existing key's life immediately.

Constraints: `agent.core` is pure (ADR-007 allowlist; time and randomness injected; no
other `agent.*` imports); no shared code with refund-api; deterministic tests.

## Options Considered

### Part 1 — Where the response classification lives

#### Option A: A pure function in `agent.core` with a strict parser — CHOSEN
- How it works: `classify_send(result, sent)` and `classify_lookup(result, sent)` take a
  plain value (`HttpResult`: status or "no response", raw body bytes, the `Retry-After`
  header) and the payload that was sent. **There are no response models**, so the core
  imports nothing from `agent.refund_client`. The body is parsed with the standard `json`
  module (allowed in core), made strict, because Python's `json` isn't:
  - **duplicate keys are rejected** (`object_pairs_hook`). Otherwise
    `{"amount_minor": 999, "amount_minor": 1000}` parses as 1000 and an ambiguous body
    confirms (measured during review);
  - **`NaN`, `Infinity` and `-Infinity` are rejected** (`parse_constant`);
  - **types are checked exactly**: `type(x) is int`, so `true` can't pass as an amount;
  - **the body is capped in size** by the HTTP client before the core ever sees it; an
    oversized body is "unparseable".
  Anything the strict parser rejects is treated as unparseable, which classifies as
  `RECONCILE` + alert on a 2xx, and never as `CONFIRMED` or `REJECTED`. The HTTP client only
  performs the call and converts transport failures into "no response".
- Pros: Unit- and property-testable without HTTP. The core purity check guarantees no I/O.
  Every branch and every parsing edge case is under the same property tests. A future
  provider adapter translates into the same `HttpResult` and reuses the classification.
- Cons: The core does a little JSON shape-checking by hand instead of through a model.
- Cost/latency/complexity profile: One module; no runtime cost.

#### Option B: `refund_client` parses into models and passes the core typed values — rejected
- How it works: Response models live in `refund_client`; it parses and hands the core either
  the fields or `UNPARSEABLE`.
- Pros: Wire models stay together in the client package.
- Cons: Two places decide what "a valid 201" means, and the parsing edge cases (a list where
  a string should be, a bool amount, a duplicate key) would sit outside the property tests.
- Why we didn't use it here: Keeping parsing and classification in one pure function puts
  every edge case under the same tests.
- When it WOULD be the better choice: Large responses where hand-checking fields would be
  error-prone.

#### Option C: Classification inside the HTTP client — rejected
- How it works: The client inspects responses and returns outcomes directly.
- Pros: Fewer types.
- Cons: Safety logic mixed with I/O; every branch needs HTTP mocks; a second adapter would
  duplicate the mapping.
- Why we didn't use it here: The mapping is the money decision; it belongs in the pure core.
- When it WOULD be the better choice: A low-stakes client where outcomes don't move money.

#### Option D: An exception hierarchy raised by the client — rejected
- How it works: `RefundRejected`, `RefundUnknown`, `RetryLater` exceptions.
- Pros: Idiomatic in some codebases.
- Cons: "Unknown" isn't exceptional, it's a normal outcome, and an unhandled exception type
  defaults to crashing or, worse, a generic handler treating it as failure.
- Why we didn't use it here: Outcomes as values. Paired with an exhaustive `match` ending in
  `assert_never` under strict mypy (which the worker uses), forgetting to handle `unknown`
  becomes a type error. Without that tooling it's only a convention.
- When it WOULD be the better choice: When failures are rare and the caller's only response
  is to abort.

### Part 2 — How the contract test validates against the spec

#### Option A: Validate real payloads with `jsonschema` against the committed spec — CHOSEN
- How it works: The test loads `contracts/refund_api.openapi.json` and builds a JSON Schema
  2020-12 validator, **with a `FormatChecker`**, using the whole spec as the root so
  `#/components/schemas/...` references resolve. It checks:
  - **requests:** anything the agent's request model accepts, the spec accepts (with
    controls showing the validator rejects the edge cases);
  - **headers:** the idempotency keys the agent generates match the spec's
    `Idempotency-Key` header schema, and the spec declares bearer authentication;
  - **responses:** every declared (path, method, status), `GET /config` included, has an
    example here that validates against the spec, parses and classifies (or is read) as the
    tables below say. Undeclared statuses (5xx, 408, 429) are handled by the default rows,
    not compared;
  - **the error-code enum** is identical in both directions.
  - **Format limits:** with a `FormatChecker`, `uuid` is checked natively but `date-time`
    is not (that needs a package we haven't added), so the test parses `created_at` with
    the standard library instead.
- Pros: A standard validator enforces patterns, enums, `additionalProperties: false` and
  bounds exactly as written. Examples test behavior, not just shape.
- Cons: One dev-only dependency (`jsonschema`). Response examples are written by hand; a
  test asserts every declared response has one.
- Cost/latency/complexity profile: Milliseconds; dev-only.

#### Option B: Compare the agent's Pydantic schemas with the spec field by field — rejected
- How it works: Generate JSON Schema from the agent's models and diff it against the spec.
- Pros: No new dependency.
- Cons: Equivalent constraints can be written differently (a pattern vs. an enum), so the
  diff has false alarms and blind spots, and it never exercises real payloads.
- Why we didn't use it here: Custom comparison code is what a standard validator replaces.
- When it WOULD be the better choice: When no new test dependency is allowed at all.

#### Option C: Generate the agent's client models from the spec — rejected
- How it works: Code generation (datamodel-code-generator) from `contracts/` at build time.
- Pros: The models can't drift.
- Cons: Generated code is shared code by another name (ADR-007 duplicates deliberately and
  tests the copies); it hides the agent's stricter types and adds a build step.
- Why we didn't use it here: It removes the independent second implementation that the
  contract test exists to compare.
- When it WOULD be the better choice: A large API with many consumers.

#### Option D: Property-based API testing against a running server (Schemathesis) — rejected for this layer
- How it works: Generate requests from the spec and fire them at a live refund-api.
- Pros: Finds server-side spec violations automatically.
- Cons: Needs a running server and database, so it belongs to e2e, and it tests the server,
  not whether the agent's client matches the spec.
- Why we didn't use it here: Wrong layer for the client contract.
- When it WOULD be the better choice: Hardening refund-api itself in the e2e suite.

### Part 3 — Retry limits

#### Option A: Bounded backoff with full jitter, a total per-attempt timeout, and a per-send age check — CHOSEN
- How it works:
  - **Backoff:** before retry *n*, wait `uniform(0, min(cap, base × 2^(n-1)))`, the draw
    injected (ADR-007). A `Retry-After` in seconds (on 429) replaces the draw, **capped at
    the backoff cap**: a narrowing of ADR-004, which caps it at the remaining window.
  - **A total timeout per attempt:** each attempt runs inside `asyncio.timeout(client_timeout)`.
    httpx's own timeouts apply per phase (connect, read, write, pool), and its read timeout
    restarts on every byte, so a trickling response could run far past `client_timeout`
    without the outer limit. httpx's timeouts stay as inner limits.
  - **The worst case:** any wait can be a capped `Retry-After`, so
    `worst_case = attempts × client_timeout + (attempts − 1) × backoff_cap`. Defaults
    (5 attempts, 8 s timeout, 0.5 s base, 8 s cap): **72 s**.
  - **The invariant that holds across crashes**, enforced by the worker before **every**
    send (not just at resume):
    1. **Age check, on the database clock:** send only if
       `now() − first_send_started_at + client_timeout ≤ effective_window`, where
       `effective_window = min(send_window_seconds, current reported window)`. The window is
       the right bound because the agent doesn't know the retention, and retention > window
       + recovery delay. If it fails, the run moves to `NEEDS_RECONCILIATION` without
       sending (ADR-008: no send after retention).
       - **The window snapshot:** `runs.send_window_seconds` is set in the same transaction
         that moves the run to `EXECUTING` and sets `first_send_started_at`, from the window
         in effect at that moment. A run therefore never gets a *longer* window than the one
         its key was sized for, and a *shrinking* window still takes effect immediately
         through the `min()`.
       - **Why it's needed:** per-key expiry (Part 5) stops a config change from shortening a
         key's life; the snapshot stops one from lengthening the agent's. Without it: a key
         stamped under a 300 s retention, a run left unclaimed during a redeploy until it's
         400 s old, the gate reopening with a new 600 s window, and a send with a key that
         expired at 300 s.
    2. **A persisted attempt count:** the total number of sends is stored on the run, so the
       attempt limit is per action, not per worker. A crash doesn't reset it. Reaching it
       also moves the run to `NEEDS_RECONCILIATION`.
    3. **`last_attempt_at` before each send:** set in the same fenced update that increments
       the count (ADR-004 step 7), because the reconciler's quiet period is measured from it.
  - **Which check is the real bound:** the **per-send age check** is the safety guarantee: it
    alone keeps every send inside retention, whatever happened before. The startup
    worst-case check (Part 4) only catches a configuration whose normal retry schedule could
    never finish inside the window, which would otherwise send every retried refund to
    reconciliation. It is not what keeps money safe.
  - **409 and 429 count as attempts.** A long `slow_commit` that keeps answering 409 (or a
    provider that keeps rate-limiting) can use up the attempts and send the run to
    reconciliation. That's intended: the reconciler then looks the refund up, after its
    quiet period, without ever re-sending. Not counting them would let a stuck provider hold
    a run in retries indefinitely.
  - The age check is a pure function in the core; the attempt-count column and the fenced
    update come with the worker.
- Pros: The guarantee no longer depends on a worker surviving its own loop. Each limit is
  checked against the database, so a crash, a resume or a slow response can't stretch it.
- Cons: Two limits (age and count) to explain; one more column on `runs`.
- Cost/latency/complexity profile: One fenced update and one comparison per send.

#### Option B: Bound only the in-memory loop (the first draft) — rejected
- How it works: A startup check on one worker's loop; the count lives in memory.
- Pros: Simplest.
- Cons: Every crash restarts the count and adds another full window; a run resumed with its
  key one second from expiry passes the resume check and keeps retrying past retention.
- Why we didn't use it here: It's the one place a refund could be sent with a key the
  provider has already forgotten.
- When it WOULD be the better choice: Calls with no idempotency-key lifetime to respect.

#### Option C: Fixed-interval retries — rejected
- How it works: Retry every N seconds.
- Pros: Simplest schedule.
- Cons: Synchronized retries from many workers hit a recovering service together.
- Why we didn't use it here: Jitter costs nothing.
- When it WOULD be the better choice: A single client and a provider that asks for a fixed
  interval.

#### Option D: A retry library (tenacity) — rejected
- How it works: Decorate the send with retry rules.
- Pros: Battle-tested.
- Cons: Whether to retry depends on the classification, the database age check and the
  worker's lease (stop immediately on a lost claim, ADR-004 step 7); a decorator sees none
  of them. Another dependency.
- Why we didn't use it here: The loop is short and its stopping rules are the point.
- When it WOULD be the better choice: Simple idempotent calls whose only stop condition is
  the attempt count.

### Part 4 — Agreeing on the retry window, and catching a wrong token

#### Option A: An authenticated `GET /config` on refund-api that gates the worker's send path — CHOSEN
- How it works: refund-api exposes `GET /config` behind the same bearer auth, returning
  `max_retry_window_seconds`, `key_retention_seconds` and `processing_budget_seconds`
  (operational settings, no secrets), declared in the committed contract. refund-api is the
  **single source** for the window; the agent no longer reads its own copy.
  - **Scope: only the send path depends on it.** The agent's API and chat readiness never
    depend on refund-api. A refund-api outage must not take customer chat or FAQ answers
    down, and a deploy must be able to start while refund-api is down. Only the worker's
    send path is gated.
  - **The worker's gate:** while the config is missing, stale or answered with 401/403, the
    worker **doesn't claim `APPROVED` or `EXECUTING` runs**. Nothing is lost: they wait, and
    the policy check still runs exactly once, at the move to `EXECUTING`, when sending
    resumes. It alerts, and a **401/403 pages**: a wrong token would otherwise turn every
    refund into an escalation.
  - **At worker startup:** a reported window smaller than the agent's worst case is
    **fatal for the worker** (the configuration could never finish a retry schedule).
    refund-api being unreachable is **not** fatal; it just means "not sending yet".
  - **Refresh:** the value is cached and refreshed on an interval (config). If a refresh
    fails, no new sends start. A run **already mid-retry** finishes its retries under the
    last known window: safe, because its age check uses `min(snapshot, last known window)`
    and, with per-key expiry (Part 5), a retention change can't shorten that run's key. If a
    refresh reports a *smaller* window, every age check uses it immediately. If a refresh
    reports a window **smaller than the worst case**, it's treated like the startup case:
    the worker stops new claims and alerts.
  - **Long outages send in-flight runs to reconciliation.** While the gate is closed,
    `EXECUTING` runs wait unclaimed and keep aging. An outage longer than a run's window
    makes its age check fail when sending resumes, so it goes to the reconciler instead of
    being sent. That's safe and intended (the reconciler looks the refund up without ever
    re-sending), not a bug.
- Pros: Enforced on every real deployment, not just in CI. One authenticated call probes the
  token and the shared value. Chat stays up when refund-api is down. The reconciler gets the
  provider's real processing budget for its quiet-period guard.
- Cons: A new endpoint in refund-api's contract; the worker's sending depends on refund-api
  being reachable (which it does anyway, to send).
- Cost/latency/complexity profile: One small authenticated call per refresh interval.

#### Option B: Gate the agent's whole readiness on `GET /config` (the pass-1 draft) — rejected
- How it works: The agent isn't ready unless the config call succeeds.
- Pros: One simple readiness rule.
- Cons: A refund-api outage pulls the whole agent from the load balancer: no chat, no FAQ
  answers, and no deploys while refund-api is down. Only sending refunds needs refund-api.
- Why we didn't use it here: It couples the customer-facing service to a dependency only the
  send path has.
- When it WOULD be the better choice: A process whose only job is sending refunds.

#### Option C: An e2e test compares the two configured values — rejected
- How it works: The e2e suite reads both services' config and asserts they match.
- Pros: No API change.
- Cons: It checks CI's configuration, not a real deployment's; the token probe still needs
  its own call.
- Why we didn't use it here: The failure it guards against is a deploy-time mistake.
- When it WOULD be the better choice: When the provider is third-party and has no config
  endpoint (as with Stripe; then the adapter hard-codes the provider's documented retention).

#### Option D: Set the value once in the deploy manifest only — rejected
- How it works: One variable, written to both services by the deploy tooling.
- Pros: The simplest.
- Cons: Nothing checks it; a manual override on one service goes unnoticed.
- Why we didn't use it here: An unchecked invariant isn't an invariant.
- When it WOULD be the better choice: Alongside Option A as the normal way to set it, never
  instead of a check.

### Part 5 — What a retention change does to keys in flight (refund-api)

#### Option A: A per-key expiry stamped at insert — CHOSEN
- How it works:
  - `idempotency_keys` gains **`expires_at`**, set **in the insert transaction on the
    database clock**: `now() + retention`, where retention comes from refund-api's config
    or, in the fault-injection build, the fault plan's override (the `key_retention` hook,
    ADR-011). The app server's clock is never used.
  - **Immutable after insert.** The runtime role already can't `UPDATE` keys (ADR-010
    privileges), so neither a bug nor a config change can lengthen or shorten a key's life.
  - **Prune:** `prune_expired_key(key)` deletes the key only when `expires_at < now()`
    **and** `created_at < now() − floor`. The floor stays in the function (an owner-only
    table, ADR-010 Part 5), not in a `CHECK` constraint, so even a bad config value or
    fault-plan override can't produce a key pruned before the floor. It still never touches
    refunds.
  - **Migration 0004:** adds the column, backfills existing rows with
    `created_at + floor`, then makes it `NOT NULL`. The floor and the retention are two
    separate settings: the floor is a minimum, and the backfill uses it only because it's
    the best value available at migration time (the seed sets it from the configured
    retention). That's safe either way: a key is pruned only once its stamped `expires_at`
    has passed **and** it's older than the floor, so lowering the floor later can never
    shorten a stamped life. If keys exist but no floor has been set, the migration fails with
    a clear message instead of guessing.
- Pros: **Structural:** a retention change only affects keys created afterwards. A key
  already in flight keeps the lifetime it was created with, which is the one the agent's age
  check was sized against. It's the same principle as Part 4: an unchecked procedure isn't an
  invariant.
- Cons: Changes how ADR-010's prune function works inside (what it promises is unchanged);
  one migration and updated prune tests.
- Cost/latency/complexity profile: One column; the same single-row delete.

#### Option B: A deploy rule — rejected
- How it works: Shrink the agent's window first, wait at least the old retention, then
  shrink refund-api's retention. Written in the runbook.
- Pros: No code.
- Cons: A procedure nobody checks; one hurried deploy shortens every in-flight key.
- Why we didn't use it here: The same weakness as Part 4's manifest-only option.
- When it WOULD be the better choice: A system whose retention is fixed by a third party and
  can't be stamped per key.

#### Option C: Keep the age-based prune (ADR-010 as first built) — rejected
- How it works: Prune compares `created_at` with the retention passed in now.
- Pros: Already built.
- Cons: Shrinking retention is retroactive: in-flight keys get the shorter life immediately,
  while the agent's age check still uses the old, larger window until its next refresh.
- Why we didn't use it here: It's a real window in which a retry can reach a forgotten key.
- When it WOULD be the better choice: When retention never changes after launch.

## Decision
Part 1 A, Part 2 A, Part 3 A, Part 4 A, Part 5 A.

### The classification (ADR-004 step 3, ADR-008, ADR-010)
| Response | Outcome |
|---|---|
| **201** whose body parses strictly and matches the sent `order_id`, `amount_minor`, `currency`, `action_id` | `CONFIRMED` (fresh or replayed). **Only 201 can confirm.** |
| 201 whose body doesn't parse strictly (duplicate keys, `NaN`, wrong types, oversized) or doesn't match | `RECONCILE` + alert |
| **Any other 2xx** (200, 202, 204…), whatever its body | `RECONCILE` + alert |
| No response (timeout, connection error, the total timeout firing) | `RETRY` |
| 5xx, 408 | `RETRY` |
| 409 (`in_flight`) | `RETRY` (wait, same key; counts as an attempt) |
| 429 | `RETRY`, honoring `Retry-After` seconds, capped at the backoff cap (counts as an attempt) |
| 400 with a recognized business code other than `invalid_request` | `REJECTED` (frees capacity) |
| 400 `invalid_request` | `RECONCILE` + alert (ADR-010 Amends 1) |
| 400 with an unrecognized or missing code, 422, 401, 403, 404, any other status | `RECONCILE` + alert |

Retries exhausted, or the age check failing → `RECONCILE` (the worker's job). The
classifiers never raise: a malformed response is an outcome.

**The reconcile lookup:** a 200 whose body parses strictly into a list in which **every**
refund matches the stored payload's `order_id`, `action_id`, `amount_minor` and `currency` →
`FOUND` (with a duplicate alert when there are two or more) or `NOT_FOUND` (empty). A row
that doesn't match → `LOOKUP_FAILED` + alert: if refund-api ever ignored a filter, the agent
must not confirm an action from someone else's refund. **Anything else is `LOOKUP_FAILED`,
never `NOT_FOUND`** (ADR-010 Amends 3).

### Why `REJECTED` is safe: an assumption, written down
A business 400 on a **retry** is trustworthy only because, within a key's lifetime, a key
that committed always replays its stored response, and business validation runs *after* the
key lookup (ADR-004, ADR-010). The per-send age check (Part 3) keeps every send inside the
window, and per-key expiry (Part 5) keeps that window from shrinking under a key in flight.
If either fails (a send past a key's life, or a provider that validates before replaying), a
business 400 could describe a second attempt while the first one committed, and "rejected"
would free capacity for money that moved.

### Where the pieces live
- `agent.core.refund_outcome`: the strict parser, the classifiers, the error-code enum, the
  backoff schedule, the worst case and the per-send age check. Pure.
- `agent.refund_client`: the agent's own **request** model, its copy of the canonical
  payload hash (tested against `contracts/hash_vectors.json`), the `RefundClient` protocol,
  `HttpRefundClient` (httpx, one attempt per call, inside the total timeout, with the
  response-size cap) and the `GET /config` client.
- The worker (step 2): the retry loop, the send gate (Part 4), the attempt-count column, the
  fenced `last_attempt_at` update, and stopping the moment the lease is lost.
- refund-api: `GET /config`, the bearer security scheme in its spec, and per-key expiry
  (migration 0004).

### Tests this ADR adds (each with a negative control)
| Test | Negative control |
|---|---|
| Property: only a recognized business code other than `invalid_request` produces `REJECTED` | A planted classifier mapping `invalid_request` (or an unknown code) to `REJECTED` is found by Hypothesis |
| Property: only a 201 matching the sent payload produces `CONFIRMED` | A planted classifier trusting any 201 is found |
| Strict parsing: a duplicate key, `NaN`/`Infinity`, a `true` amount, an oversized body → never `CONFIRMED` | With plain `json.loads`, the duplicate-key body (`999` then `1000`) comes out `CONFIRMED` |
| Property: no lookup error produces `NOT_FOUND` | A planted classifier mapping a 500 to not-found is found |
| Generator check: the properties' generator actually reaches `REJECTED` and `CONFIRMED` | (This *is* the control against a vacuous property) |
| A lookup returning a refund with a different amount, currency, order or action → `LOOKUP_FAILED` + alert | Without the full match, it comes out `FOUND` |
| Worst case: no sequence of `Retry-After` hints and jitter makes the waits exceed the computed bound | The old formula (early ceilings instead of the cap) is exceeded by a found sequence |
| A trickling response is cut off at the total timeout and returns "no response" | httpx's per-phase timeouts alone let it overrun |
| Per-send age check: a run resumed close to the window doesn't send | Without the check, it sends |
| Window snapshot: a run started under a small window, the window then raised, resumed past the old window → no send, `NEEDS_RECONCILIATION` (worker step) | An age check against the current window only sends it |
| A refreshed window below the worst case stops new claims and alerts (worker step) | A refreshed window that fits keeps sending |
| The attempt count survives a crash and resume (worker step) | An in-memory count restarts and sends again |
| Config missing, stale or 401 → the worker claims nothing; a 401 pages (worker step) | A worker without the gate claims and sends |
| refund-api down → customer chat still works and approved refunds wait unclaimed (**in EVAL_PLAN; lands with the chat API, step 7**) | Whole-agent readiness on `GET /config` takes chat down too |
| A reported window smaller than the worst case is fatal for the worker; unreachable is not | A window that fits starts sending |
| Per-key expiry: shrinking retention after a key is created leaves its `expires_at` unchanged, and its replay still works | The old age-based prune deletes it early |
| The runtime role can't `UPDATE` a key's `expires_at` | The owner can |
| The floor still holds: a key whose `expires_at` has passed but is younger than the floor isn't pruned | Without the floor condition, it's pruned |
| Migration 0004 backfills `expires_at = created_at + floor`, and refuses when keys exist but no floor is set | (The refusal is the control) |
| Every declared response, `GET /config` included, has an example that validates, parses and classifies | An example removed from the table fails the coverage check |
| Requests: anything the agent's model accepts, the spec accepts; generated keys match the header schema | The validator rejects the listed edge cases (so it isn't accepting everything) |
| The error-code enum matches both ways | A spec copy with an extra code is caught |

## Consequences
- The hash-vector placeholder becomes the real test.
- A new refund-api error code fails the agent's contract test until the agent's enum and
  classification handle it.
- refund-api gains `GET /config` in its contract and declares bearer authentication.
- `REFUND_API_MAX_RETRY_WINDOW_SECONDS` becomes refund-api-only; the agent reads the value
  from `GET /config`.
- refund-api gains migration 0004 (`expires_at`) and a new prune signature; its fault build's
  retention override now sets the stamped lifetime.
- `runs` gains a send-attempt count and `send_window_seconds` (worker step).
- An outage of refund-api longer than a run's window sends that `EXECUTING` run to the
  reconciler when sending resumes, instead of sending it. Intended, not a bug.
- **The reconciler (step 4) gets a quiet-period guard** at startup:
  `quiet_period > client_timeout + processing_budget + margin`, with the processing budget
  taken from `GET /config`.
- EVAL_PLAN gains the "refund-api down, chat still works" row.
- Pointer lines go into ADR-004 (the `Retry-After` narrowing), ADR-010 (`GET /config`,
  per-key expiry) and ADR-011 (the `key_retention` hook's new meaning) when this ADR is
  locked.
- **Revisit if:** a provider adapter arrives (it must guarantee replay-before-validation for
  business errors, or its business 400s can't map to `REJECTED`; and without a config
  endpoint it hard-codes the provider's documented retention), or refund-api starts sending
  `Retry-After` as an HTTP date.

## Interview-ready summary
"The single most dangerous function on the agent side turns an HTTP response into
'refunded', 'rejected' or 'don't know', so it's a pure function in my core package, with a
strict parser, because Python's JSON parser accepts duplicate keys and NaN, and I measured a
duplicate-key body confirming a refund. Reviews caught three more things. My retry limit only
bounded one worker's loop, so a crash restarted it; now the worker checks the key's age on
the database clock before every send, and the attempt count lives on the run. My worst-case
window assumed small early waits, when any wait can be a capped Retry-After. And shrinking
the provider's retention would have shortened keys already in flight, so each key now
carries its own expiry, stamped on the database clock and immutable. Per-key expiry stops a
config change from shortening a key's life, and a per-run window snapshot stops it from
lengthening the agent's. The agent reads the
provider's window from an authenticated config endpoint, which also catches a wrong token,
but only the worker's send path depends on it: if the refund service is down, chat stays up
and approved refunds simply wait."

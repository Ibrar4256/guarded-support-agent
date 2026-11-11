# ADR-011: refund-api Test-Build Wiring

**Status:** Accepted (2026-11-12). Revised the same day after outside review: the Pros and
Cons corrected, the production entry point tested directly (wiring and hooks), hook
positions pinned to ADR-010's lock order, the retention-floor interaction, and auth on the
test routes stated.

**Locked 2026-11-12.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Amended by [ADR-012](012-refund-client-classification-and-contract.md):** the
`key_retention` hook sets the lifetime stamped on a new key's `expires_at`, instead of the
retention passed to `prune_expired_key`.

**Precedence:** narrows ADR-007's "test-only code location" rule for refund-api only. The
agent keeps ADR-007's flag-gated factory (its scripted model is selected by configuration
inside one running service). Nothing in ADR-010 changes.

## Context
ADR-010 Part 3 needs a fault-injection build of refund-api: fault plans per `action_id`, a
test-only charge endpoint, and fault hooks, all present **only** when
`DEBUG_FAULT_INJECTION=true`, and never in a production image. ADR-007 put test-only code in
`testing/refund_api_testing/`, outside `src/`, and forbids production code from importing
it. For the agent, ADR-007 allows one exception: a production factory that lazily imports
the test package behind a flag.

The question is how the test build gets assembled for refund-api: from production code,
or from the test package.

Constraints: no test code reachable in a production image (ADR-007's image test), the
production gate (ADR-004/006), and import contracts with negative controls.

## Options Considered

### Option A: A test entry point in `refund_api_testing` that wraps the production app — CHOSEN
- How it works: `refund_api_testing.app:create_fault_app` calls the production
  `create_app(settings, hooks)` with fault hooks, then mounts the test routes (fault plans,
  charges, reset) and a handler for injected faults. It refuses to start unless
  `DEBUG_FAULT_INJECTION=true` and `APP_ENV` isn't `production`. Test runs start uvicorn
  with `--factory refund_api_testing.app:fault_app_from_env`.
- Pros: Imports only go from test code to production code, never the other way, so the
  existing "production never imports test code" contract holds with **no exception**. A
  production image doesn't contain `refund_api_testing`, so the test build can't even be
  started there. Production source has no test-code path to review. And a misconfigured
  production start with the flag set fails loudly (the production gate) rather than
  silently ignoring it.
- Cons: **The two entry points can drift.** If tests only ever used the fault build, bugs in
  the production entry point's own wiring (settings loading, middleware order) would never
  be exercised. **A test run could start the production entry point by mistake,** so fault
  tests would run with no faults; ADR-010's applied-check (every fault test asserts its
  fault fired) turns that into a failure rather than a silent pass.
- Cost/latency/complexity profile: One small factory module; nothing at runtime.

### Option B: A flag-gated lazy import in the production factory (ADR-007's agent pattern) — rejected
- How it works: `refund_api/app.py` checks `DEBUG_FAULT_INJECTION` and, if set, imports
  `refund_api_testing` inside that branch to install fault hooks and routes. The import
  contract gets an `ignore_imports` exception for that module.
- Pros: One entry point; the same pattern as the agent.
- Cons: Production source contains a path to test code, guarded only by a flag and an
  import-linter exception that has to be kept narrow. A production image would fail at
  startup if the flag were ever set there, rather than not having the path at all.
- Why we didn't use it here: Option A gives the same behavior with a stricter boundary.
- When it WOULD be the better choice: When the test variant must be selected inside one
  running process by configuration, as with the agent's scripted model provider, where a
  separate entry point would duplicate the whole service wiring.

### Option C: A separate test-only service image that subclasses the app — rejected
- How it works: A different Dockerfile builds a "refund-api-test" image whose own module
  constructs the app with fault behavior.
- Pros: Total separation of artifacts.
- Cons: A second image to build and keep in sync, for what Option A does with one module
  and one build target.
- Why we didn't use it here: The multi-stage Dockerfile's `test` target already separates
  the artifacts.
- When it WOULD be the better choice: When test and production builds must come from
  different pipelines or registries for compliance reasons.

## Decision
Option A. The production app exposes hook seams (ADR-007's injection seams, default no-op),
each given the request's idempotency key and `action_id`. `refund_api_testing` supplies
`FaultHooks`, which read the fault plans, and the test routes. Production code has no
`if testing` branch.

### Two thin factories, one difference
- `refund_api.app:app_from_env` = `create_app(Settings.from_env(environ))`, which always uses
  the fixed no-op `RefundHooks`. It **never builds hooks from environment or config.**
- `refund_api_testing.app:fault_app_from_env` = the same `create_app` call with `FaultHooks`,
  plus the test routes and the injected-fault handler.
- **Which build each test uses:** every non-fault test (protocol, auth, validation, races,
  deadline) builds the production `create_app`. A test also goes through `app_from_env`
  itself, from environment variables to a committed refund, and checks the middleware order
  (receipt outermost, then auth; ADR-010). Only the fault tests use the fault build. The
  e2e happy path runs against `app_from_env` when `tests/e2e` exists.
- **Guarding the dangerous seam:** `deduplicates` can switch off idempotency. A test asserts
  the production entry point's hooks are exactly `RefundHooks`; its negative controls are the
  fault build, and any other `RefundHooks` subclass, failing the same check.
- **`key_retention` can't go below the floor.** An override still goes through
  `prune_expired_key`, which never deletes a key younger than the owner-set floor
  (ADR-010 Part 5). That's why the test database sets the floor small: the floor working as
  intended, not a bypass.

### Where each hook runs (ADR-010's order)
| Hook | Runs | Sync/async | Used by |
|---|---|---|---|
| `before_transaction` | After receipt, before a connection is taken, so before the advisory lock | async | the deadline test (time before the transaction counts) |
| `deduplicates` | Inside the transaction, before the advisory lock. If false, **no key lock is taken at all** and no key is stored | sync | `dedup_disabled` |
| `lock_key` | Computes the advisory-lock ID, taken before anything else in the key path | sync | the forced-collision 409 test |
| `key_retention` | After the lock, as the retention given to `prune_expired_key` (the floor still applies) | sync | the per-action retention override |
| `before_commit` | After the lock, with every row written, **while the locks are held** | async | `slow_commit`, `fail_before_commit`; so a concurrent same-key request gets 409 before any delay matters |
| `after_commit` | **After the commit, when the locks are released**, before the response | async | `fail_after_commit`, `timeout_after_commit`; during the hang a retry gets a replay, not a 409, as with a real provider that has committed |

Hooks that wait are async, so a delay never blocks the event loop; hooks that only return a
value are sync.

### Test routes are behind auth
The test routes are mounted on the wrapped app, so they sit behind the same bearer-token
middleware as everything else. The test harness sends a token, and a test asserts an
unauthenticated call to a test route gets 401. No bypass may be added.

## Consequences
- Two uvicorn entry points: `refund_api.app:app_from_env` (production) and
  `refund_api_testing.app:fault_app_from_env` (fault injection). The e2e compose file uses
  the second with a single worker (ADR-010 Part 3).
- The import contracts stay exception-free for refund-api. `refund_api_testing` is already in
  import-linter's `root_packages` (so the contract can see it), with the planted-import
  negative control from step 1.
- The production-image test asserts `import refund_api_testing` fails in the `production`
  build target; its negative control is the same import succeeding in the `test` target.
- **Revisit if:** refund-api ever needs test behavior selected at runtime inside a
  production-shaped process (then Option B).

## Interview-ready summary
"For the refund service's fault-injection build, I flipped the usual pattern. Instead of
production code importing test code behind a flag, the test package imports the production
app and wraps it: it swaps in fault hooks and mounts test-only routes. Imports only go from
test code to production, so my 'production never imports test code' rule needs no
exception, and a production image can't start the test build because the package isn't in
it. The agent still uses a flag-gated factory, because its fake model has to be chosen by
configuration inside one running service; there a separate entry point would duplicate the
whole wiring."

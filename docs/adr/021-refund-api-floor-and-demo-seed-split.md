# ADR-021: refund-api's Key-Retention Floor on Every Deployment, Demo Charges Only on a Demo

**Status:** Accepted (2026-11-14). Part 1 A, S5 (message text only) and the command name chosen
by the user in chat. Revised the same day after one outside review: refund-api refuses to start
without a floor (Part 2, S8), what happens if the floor disappears after startup (S9), and why the
two processes' retention settings needn't match (S10). S8 (refuse to start when the database is
unreachable) chosen by the user.

**Locked 2026-11-14.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Precedence:** changes how ADR-010 Part 2 ("refund-api's seed command writes charges") and Part 5
("the owner role sets the floor at deploy time") are carried out; their rules are unchanged. Brings
refund-api's seed in line with ADR-019 S6.

## Context
`python -m refund_api.seed <manifest>` (owner role, `REFUND_DB_OWNER_URL`) does two jobs in one
transaction:

1. inserts the demo manifest's charges (refusing any difference in an existing one), and
2. sets the key-retention floor (`key_retention_policy`) from `REFUND_API_KEY_RETENTION_SECONDS`.
   Without a floor, `prune_expired_key` raises (migration 0002/0004) and every refund fails
   closed. A real deployment needs this.

ADR-019 S6 made the agent's seed refuse (exit 2, nothing written) unless `DEMO_SEED_ENABLED=true`,
so a real deployment's deploy step can't insert fake data by accident. refund-api's seed can't
take the same guard as it stands: refusing would also skip the floor, and a real deployment would
then fail every refund. `KNOWN_TRADEOFFS.md` records this ("refund-api's seed has no demo guard
yet").

Constraints: the floor is written by the owner role only; the runtime role has no privileges on
`key_retention_policy` (ADR-010 Part 5, by design). No shared code with the agent (ADR-007). The
manifest stays the single input for demo data (ADR-010 Part 2). `tests/e2e/conftest.py` runs the
seed in its session setup, because the crash tests need the floor.

## Options Considered

### Part 1 — Where the floor is set

#### Option A: Its own command, run on every deployment; the seed becomes charges-only and demo-gated — CHOSEN
- How it works: a new `python -m refund_api.retention_floor` (no arguments, owner role) sets the
  floor from `REFUND_API_KEY_RETENTION_SECONDS`, nothing else. `python -m refund_api.seed
  <manifest>` keeps the charge logic, stops touching the floor, and refuses (exit 2, nothing
  written) unless `DEMO_SEED_ENABLED=true`, exactly as the agent's seed does. Deploy steps:
  every deployment runs migrations, then the floor command; a demo deployment also runs both seeds.
- Pros: each command does one job; a real deployment needs no manifest; the same flag means the
  same thing in both services (exit 2 when not a demo); the floor has one writer.
- Cons: one more deploy command. A deploy that forgets it is stopped when refund-api starts
  (Part 2), not silently.
- Cost/latency/complexity profile: a ~40-line module; the seed loses code.

#### Option B: One command; the flag gates only the charges — rejected
- How it works: the seed always sets the floor; without `DEMO_SEED_ENABLED=true` it skips the
  charges and exits 0.
- Pros: one deploy command; the floor can't be forgotten.
- Cons: a real deployment must ship and pass a demo manifest just to set a production setting; the
  same flag exits 2 in the agent's seed and 0 here; a demo deploy that forgot the flag gets no
  charges and still reports success.
- Why we didn't use it here: it keeps demo data and a production setting coupled, which is the
  problem being fixed.
- When it WOULD be the better choice: a deployment tool that can run only one command per service.

#### Option C: Alembic sets the floor on every `upgrade` (a post-upgrade hook in `env.py`) — rejected
- Pros: migrations always run, so the floor is never forgotten.
- Cons: `upgrade head` at head is a no-op, so a hook is hidden behavior outside the revision
  history; migrations would read a runtime setting, so the same revisions give different databases.
- Why we didn't use it here: schema history should be reproducible; a setting isn't schema.
- When it WOULD be the better choice: never for a value that changes per environment.

#### Option D: refund-api sets the floor at startup — rejected
- Pros: no deploy step at all.
- Cons: needs the runtime role to write `key_retention_policy`, which ADR-010 Part 5 forbids: the
  floor exists so application code can't lower it.
- Why we didn't use it here: it removes the protection the floor provides.
- When it WOULD be the better choice: never while the floor guards against app bugs.

### Part 2 — Catching a missing floor before the first refund

Without Part 2, a deploy that skipped the floor command is safe but found late: refund-api starts,
the first `POST /refunds` gets a 500 (prune raises `55000`), the agent counts it as a provider-type
failure, its write breaker opens and alerts, and it pages after
`PROVIDER_BREAKER_PAGE_AFTER_SECONDS` (ADR-018). The page says "refund-api's write path is
failing", not "a deploy step was skipped".

#### Option A: A read-only `SECURITY DEFINER` check, called once at startup — CHOSEN
- How it works: migration 0005 adds `public.retention_floor_is_set() RETURNS boolean`, owned by
  the owner role, `SECURITY DEFINER`, `search_path` pinned, `EXECUTE` revoked from `PUBLIC` and
  granted to the app group `refund_api_app` only (the prune function's hardening, ADR-010 Part 5).
  `create_app` gains a lifespan that calls it before serving; `false` raises with a message naming
  `python -m refund_api.retention_floor`, so uvicorn exits at deploy time. Both builds get it (the
  fault build wraps `create_app`, ADR-011).
- Pros: the failure happens at the step that was skipped, with a message naming the fix; the
  runtime role still can't read or write the floor table; with the documented order (migrations →
  floor → start) a correct deploy never hits it.
- Cons: one more privileged object to review (`KNOWN_TRADEOFFS.md` already lists these); one query
  at startup.
- Cost/latency/complexity profile: a five-line function, a migration, a lifespan hook.

#### Option B: Probe with `prune_expired_key` on a key that can't exist — rejected
- Pros: no migration; it raises exactly when a refund would.
- Cons: a startup check that calls a deleting function for its error; it depends on prune's
  internals, so a later change to prune could make the check pass silently.
- Why we didn't use it here: a check should say what it checks.
- When it WOULD be the better choice: a schema that can't take new migrations.

#### Option C: Grant the app group `SELECT` on `key_retention_policy` — rejected
- Pros: no function; a plain query.
- Cons: contradicts ADR-010 Part 5's "no privileges for the app group" on the floor table, which
  its tests assert; it would need a superseding ADR for no gain.
- Why we didn't use it here: Option A answers the same question without widening the role.
- When it WOULD be the better choice: if the app ever needs the floor's value, not just its presence.

#### Option D: Rely on the deploy script and the README — rejected
- Pros: nothing to build.
- Cons: a hand-run or partial deploy still finds out at the first refund.
- Why we didn't use it here: the cost of Option A is small and the failure moves to the right step.
- When it WOULD be the better choice: never, once the check is this cheap.

### Smaller decisions
- **S1. One writer of the floor:** only `refund_api.retention_floor` writes `key_retention_policy`.
  The seed no longer reads `REFUND_API_KEY_RETENTION_SECONDS`.
- **S2. Same guard as ADR-019 S6:** `DEMO_SEED_ENABLED` must be exactly `true` (missing, empty,
  `false`, `1`, `TRUE` all refuse). It's checked before the manifest is read or the database is
  touched. One flag name across both services is shared configuration, not shared code (ADR-007).
- **S3. The floor command:** no arguments (any argument: usage, exit 2); retention must be a
  positive number of seconds (exit 1 otherwise, nothing written); a re-run replaces the floor,
  including lowering it, which is the owner's decision as it is today. It prints the floor it set.
  It doesn't run refund-api's full startup validation (that needs tokens and every app setting for
  a database-only step); the app's own startup guard still checks retention against the window.
- **S4. Exit codes:** floor command 0 set, 1 refused, 2 usage. Seed 0 done, 1 refused, 2 usage or
  not a demo deployment (as ADR-019 S4).
- **S5. Migration 0004's refusal message** names the floor command instead of the seed. Only the
  message string changes (no schema effect, same revision), so an operator upgrading an old
  database is pointed at the right command.
- **S6. e2e harness:** session setup runs the floor command, then the seed with
  `DEMO_SEED_ENABLED=true`, the demo deploy order. The flag lives in `tests/e2e/profile.py` next to
  the retention it seeds with.
- **S7. Deploy order in the README:** every deployment: migrations, then the floor, then start
  refund-api (Part 2 refuses to start otherwise). A demo additionally: both seeds, before starting
  the worker (ADR-019).
- **S8. A database refund-api can't reach at startup also stops the start.** Today refund-api
  connects lazily and can start with its database down (its refunds fail with 5xx until it's back).
  With Part 2 the startup check needs the database, so an unreachable one fails the start too,
  with its own message (not the floor message). The check runs under `asyncio.timeout` of the pool
  timeout, since the pool timeout alone doesn't bound a connect. Failing fast lets the
  orchestrator restart it; starting anyway would skip the check exactly when nobody is watching.
- **S9. A floor removed after startup** (an owner deleting the row) isn't seen by the startup
  check; that case keeps the path above: prune raises, refund-api answers 500, the agent's write
  breaker opens, alerts, and pages. It's covered either way, at startup or at the first refund.
- **S10. The two processes' retention settings needn't match.** The floor command and refund-api
  both read `REFUND_API_KEY_RETENTION_SECONDS`, in different processes, so a deploy could give
  them different values. That's safe: prune deletes a key only when its stamped `expires_at`
  (from the app's setting) has passed **and** it is older than the floor (ADR-012 Part 5), so a
  key lives for the longer of the two. No mechanism to keep them in sync is needed.

## Decision
Part 1 A, Part 2 A, S1–S10.

### Tests (each with a negative control)
| Test | Negative control |
|---|---|
| The floor command sets the floor to the configured retention, with no `DEMO_SEED_ENABLED` and no manifest | Guarding the floor command with the demo flag leaves no floor |
| A re-run with a different retention replaces the floor | An insert-if-missing floor keeps the old value |
| Missing, non-numeric, zero or negative retention: exit 1, existing floor unchanged | Dropping the positive check: zero reaches the database and the error isn't the command's refusal |
| Any argument: exit 2, nothing written | — |
| The seed without `DEMO_SEED_ENABLED=true` (missing, empty, `false`, `1`, `TRUE`) writes no charges, exit 2 | Not checking the flag seeds anyway |
| The seed with the flag inserts charges and leaves the floor as it was (set or absent) | A seed that still writes the floor changes it |
| Existing seed tests (idempotent re-run, a mismatched charge refuses everything, a seeded charge can be refunded) keep passing with the flag | — |
| The e2e harness runs both commands; its refunds succeed only with the floor set | Dropping the floor command from the harness fails refunds closed |
| refund-api (real uvicorn, production factory) with no floor exits non-zero within a bound, naming `python -m refund_api.retention_floor`; the fault build too | With the floor set, it starts and answers `GET /config` |
| The startup check runs in the lifespan, not only in a test helper | Removing the lifespan call lets a floorless refund-api start |
| `retention_floor_is_set()`: only `refund_api_app` can execute it (not `PUBLIC`, not `agent_runtime`); `SECURITY DEFINER` with a pinned `search_path`; it returns `true`/`false` as the floor is set or not | The owner can execute it; dropping the revoke lets another role call it |
| The runtime role still can't read or write `key_retention_policy` (existing test, unchanged) | — |
| An unreachable database at startup fails the start with its own message, within the startup timeout | — |

## Consequences
- New deploy command `python -m refund_api.retention_floor` as `REFUND_DB_OWNER_URL`, on every
  deployment; `refund_api.seed` is demo-only and charge-only.
- Migration 0005 (`retention_floor_is_set()`); refund-api checks it at startup and no longer starts
  without its database (S8).
- `KNOWN_TRADEOFFS.md`: the "refund-api's seed has no demo guard yet" entry is resolved; new
  entries: a deploy that skips the floor command stops at refund-api's start (and a floor removed
  later fails refunds closed, S9); refund-api no longer starts with its database down (S8).
- README deploy steps and `tests/e2e/conftest.py` updated; pointer line in ADR-010 and ADR-019.
- **Revisit if:** the deployment tool can run only one command per service (then Option B); a
  second production setting needs an owner-only write (then one `refund_api.deploy_settings`
  command for all of them, still separate from demo data).

## Interview-ready summary
"Our provider's seed did two jobs: it inserted demo charges and set a retention floor that
production needs, because without the floor every refund fails closed. When we made the seeds
refuse unless the deployment is a demo, that coupling became a trap: guard the command and
production loses its floor; don't guard it and production gets fake charges. We split it. A
setting every deployment needs got its own command, and the demo seed only writes demo data.
The rule I took from it: a command that mixes production configuration with fixture data can't be
safely gated by either."

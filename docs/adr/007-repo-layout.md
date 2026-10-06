# ADR-007: Repository Layout and Dependency Management

**Status:** Accepted (2026-10-06). Revised the same day before locking: test-code
packaging and lazy import, one negative control per contract, a `core/` allowlist with
injected time and randomness, error codes enforced in both directions, shape vs.
behavior, CI scope, and a pinned uv version.

**Locked 2026-10-06.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

## Context
ADR-004 requires the agent and refund-api to be **separate systems**: separate processes,
databases and credentials, communicating only over HTTP. That boundary has to exist in the
code too, or a single `from refund_api import …` inside the agent quietly undoes it.

Other constraints: two Python services plus a Next.js frontend, one developer, one CI
pipeline, and the ADR-005 / ADR-006 test infrastructure (scripted model, fault injection)
must never reach a production image.

## Options Considered

### Option A: Monorepo with independent apps, no shared Python package — CHOSEN
- How it works: One repository. `services/agent/`, `services/refund_api/` and `web/` are
  independent apps, each with its own dependency file, lockfile, Dockerfile and (for the
  Python services) Alembic environment. The services share **no Python package**. Their only
  contract is HTTP, described by refund-api's OpenAPI spec.
- Pros: One clone, one CI pipeline, and atomic commits when a change touches both sides of
  the HTTP contract. Each service can be built and deployed on its own. The boundary is
  enforced by tooling (below), not by convention.
- Cons: Small amounts of duplicated code (for example the refund request and response
  models exist on both sides). More configuration files than a single package.
- Cost/latency/complexity profile: No runtime cost. A few extra CI steps (import contracts,
  checks, contract test), each taking seconds.

### Option B: One Python package containing both services as modules — rejected
- How it works: A single `pyproject.toml`, with `app/agent/` and `app/refund_api/` as
  subpackages and shared utilities between them.
- Pros: The simplest setup: one environment, one lockfile, shared helpers.
- Cons: Importing across the boundary is one line, and nothing stops it. Both services ship
  each other's dependencies. A shared helper makes refund-api's behavior depend on agent
  code, which undermines the "separate system" assumption of the crash tests.
- Why we didn't use it here: It makes ADR-004's boundary a convention.
- When it WOULD be the better choice: A small, single-service app where internal modules
  aren't meant to fail independently.

### Option C: Separate repositories per service — rejected
- How it works: The agent, refund-api and the frontend each live in their own repo, with
  their own CI.
- Pros: The strongest isolation; independent release cycles and permissions.
- Cons: A change to the refund contract means coordinated PRs across repos. Three CI setups
  to maintain. Harder to review and present as one portfolio project.
- Why we didn't use it here: All the overhead of multi-team tooling, with one developer.
- When it WOULD be the better choice: Multiple teams owning different services, with
  separate release schedules and access control.

### Option D: JS monorepo tooling (Turborepo / Nx) over everything — rejected
- How it works: A JS build orchestrator manages tasks and caching for all apps, including
  the Python services through custom task definitions.
- Pros: Task caching, affected-only builds, a dependency graph view.
- Cons: Two of the three apps are Python, so most tasks would be shell wrappers. Adds Node
  tooling to Python CI. Its caching benefits don't matter at this size.
- Why we didn't use it here: Heavy tooling for a repo that's mostly Python.
- When it WOULD be the better choice: A JS/TS-heavy monorepo with many packages and apps
  where build caching saves real time.

## Decision
**A**, with these rules.

### Layout
```
services/
  agent/
    pyproject.toml  uv.lock  Dockerfile  alembic.ini  migrations/
    src/agent/
      core/           # pure domain logic: state transitions, policy check, capacity
                      # calculation, reply templates. No I/O imports.
      db/             # SQLAlchemy Core tables and queries (ADR-005)
      llm/            # provider interface + async adapter (ported from Project 1)
      refund_client/  # RefundClient interface + HTTP implementation, own models
      api/            # FastAPI routes, SSE (ADR-003)
      worker/         # claim, lease, heartbeat, execution (ADR-004)
    testing/agent_testing/   # test-only package: ScriptedLLM, crash and race hooks
    tests/                   # unit + service-level integration tests
  refund_api/
    pyproject.toml  uv.lock  Dockerfile  alembic.ini  migrations/
    src/refund_api/
    testing/refund_api_testing/
    tests/
web/                  # Next.js + TypeScript
contracts/            # DATA, not code: OpenAPI spec, error-code list, hash test vectors
  refund_api.openapi.json
  hash_vectors.json
tests/e2e/            # cross-service tests: crash tests, races, the full refund flow
docker-compose.yml    # both services + Postgres; used locally and by tests/e2e
.github/workflows/
docs/
```

### Boundary enforcement
1. **Import contracts (`import-linter`, run in CI):**
   - *Independence:* `agent` must not import `refund_api`, and the reverse.
   - *Core purity, as an **allowlist**:* `agent.core` may import **only** an explicit set
     of standard-library modules (`typing`, `dataclasses`, `enum`, `datetime` for types,
     `hashlib`, `json`) plus `pydantic`, and no other `agent.*` package. A denylist would
     let a new I/O library slip through. import-linter's built-in contracts are
     denylists, so this check is a small AST-based CI script (or a custom import-linter
     contract) that fails on any import outside the allowlist.
   - *Time and randomness are injected:* `core` never calls `datetime.now()`,
     `time.time()`, `uuid4()` or `random`. The current time, lease deadlines and new IDs
     or idempotency keys arrive as **parameters** from the caller. The same AST check
     bans those calls in `core`. Without this, "pure" would be weaker than it reads.
   - *Test code isolation:* nothing under `src/` imports `agent_testing`, except the
     provider factory, behind the ADR-006 flag gate. `agent_testing` is listed in
     import-linter's `root_packages` (together with `agent`), so the contract actually
     sees it.
2. **Dependency check:** neither service's `pyproject.toml` lists the other as a
   dependency (a CI script checks this).
3. **Docker check:** each service's build context is its **own directory**, so its
   Dockerfile can't `COPY` the other service. A CI script also fails if either Dockerfile
   references the other service's path.
4. **Negative controls, one per contract:** a CI step plants a violation in a temporary
   copy and asserts the check **fails**, separately for each contract:
   - `agent` importing `refund_api` (independence),
   - `agent.core` importing `sqlalchemy`, and `agent.core` calling `datetime.now()`
     (core allowlist and injected time),
   - a module under `src/` importing `agent_testing` outside the factory (test code
     isolation).
   One planted violation only proves one contract works; a misconfigured contract would
   otherwise pass silently (the same principle as ADR-004's `dedup_disabled`).

### No shared package: duplicate small things
- The services share no Python code. Small things, such as the refund request and
  response models, money helpers, and canonical-JSON hashing (ADR-004 step 8), are
  **duplicated** on purpose.
- Duplication is kept honest by the contract test, not by sharing code.

### Contract test (the HTTP boundary)
- refund-api exports its OpenAPI spec to `contracts/refund_api.openapi.json`. CI
  regenerates it and **fails if it differs from the committed file**, so a contract change
  is always a visible diff in review.
- The agent's `refund_client` models are validated against that file in a test: every
  request the agent can send must validate against the spec's request schemas, and every
  response the spec defines (2xx, 400 business error, 409, 422) must parse into the agent's
  models.
- **Error codes in both directions:** refund-api's business-error codes (for example
  `refund_exceeds_charge`) are an **enum in the spec**, and a test asserts the agent's
  models list **exactly the same set**. A new code added to refund-api fails CI until the
  agent handles it.
- **Unrecognized responses are `unknown`, never `rejected`:** any status code or error
  code the agent's models don't recognize is treated as an unknown outcome and raises an
  alert (ADR-004). Only a recognized business-error code can set `rejected`.
- Canonical-payload hashing is checked on both sides with shared **test vectors** (input
  → expected hash) stored in `contracts/`, so the duplicated implementations can't
  diverge.
- **Shape vs. behavior:** FastAPI generates the spec from code, so the OpenAPI diff only
  proves the spec matches the code's **shape**. Whether refund-api actually **behaves**
  that way (409 while in flight, 422 on payload mismatch, replay of stored responses) is
  checked by ADR-004's failure-mode tests.
- `contracts/` is **data, not code** (JSON files). It's read by tests on both sides and
  isn't a shared package, so it doesn't break the no-shared-code rule.

### Test-only code location
- Each service's test-only package (★ `agent_testing`, `refund_api_testing`, named per
  service so tracebacks and linter config can't mix them up) sits in `testing/`, **outside
  `src/`**. It's importable only in test runs, through pytest's `pythonpath` setting
  (`["src", "testing"]`), and in the CI test image. The production Docker target doesn't
  copy it, so `ScriptedLLM` and fault-injection hooks **don't exist** in a production
  image. That's a third layer on top of ADR-006's factory gate and startup check.
- **The factory imports test code lazily**, inside the flag-gated branch only. A top-level
  import would crash the production service at startup, because the package isn't there.
- **Hooks are injected, not branched:** the crash points (ADR-004) and the race hook
  (ADR-005) live in `agent_testing`. Production code exposes **injection seams**: a hooks
  object whose default methods do nothing, passed into the worker and the check
  transaction. There's no `if DEBUG:` inside production logic. The gated factory installs
  the test hooks; the `CRASH_POINT` and `DEBUG_FAULT_INJECTION` variables are read there.
- **Image test:** build the production Docker target and assert that `python -c "import
  agent_testing"` fails inside it.

### Dependency management: uv
- **uv**, with an **independent `uv.lock` per service** (no uv workspace), so each service
  resolves and locks its own dependencies, the same way they'll be built.
- CI and Docker install with **`uv sync --locked`**, which fails if the lockfile doesn't
  match `pyproject.toml`, so builds always use exactly the locked versions.
- **Fallback:** if uv becomes a problem, `uv export` produces a pinned `requirements.txt`
  per service, installable with plain `pip`. Moving off uv is a mechanical change.
- **The uv version is pinned** in the repo: `[tool.uv] required-version` in each
  `pyproject.toml`, the same version in the CI setup step, and in the Dockerfile's uv
  image tag. A uv update can't change how dependencies resolve.
- Installing uv on the development machine needs the user's explicit OK (done at
  implementation time).

### CI scope
- **Per service, independently:** lint, type check, unit tests and `uv sync --locked`.
  The jobs run in parallel, and a failure names the service.
- **Root job, after both pass:** import contracts with their negative controls, the
  dependency and Docker checks, the contract test, the production-image test, and
  `tests/e2e/` against `docker-compose.yml` (the crash tests and races need both services
  and Postgres).

## Consequences
- More files to maintain: two `pyproject.toml` files, two lockfiles, two Alembic setups,
  and the contract file.
- Some code exists twice; the contract test and shared test vectors are what keep it in
  sync.
- `web/` follows its own toolchain (npm/pnpm) and is decided when the frontend starts.
- **Revisit if:** a third Python service appears (then a small shared, versioned package
  for truly common code may pay off), or duplication between the services grows large
  enough to cause real bugs or effort.

## Interview-ready summary
"It's a monorepo with independent apps: the agent and the refund service each have their
own dependencies, lockfile, Dockerfile and migrations, and share no Python code. The only
contract between them is HTTP, and it's enforced, not just documented. Import-linter
forbids cross-imports, each Docker build context is the service's own folder, and a
negative-control step proves the linter actually fails on a violation. Small things like
the refund models are deliberately duplicated, and a contract test against the committed
OpenAPI spec plus shared hash test vectors keeps the copies in sync. Domain logic sits in
a pure core package with no I/O imports, so policy rules are unit-testable and can't be
bypassed. Test-only code like the scripted model lives outside src and isn't in the
production image at all. A single package would have been simpler, but it would turn the
service boundary my crash tests rely on into a convention. Separate repos would make sense
with multiple teams."

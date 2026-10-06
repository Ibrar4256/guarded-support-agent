# ADR-007: Repository Layout and Dependency Management

**Status:** Accepted (2026-10-06)

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
    testing/          # test-only code: ScriptedLLM, fault-injection hooks (ADR-005/006)
    tests/
  refund_api/
    pyproject.toml  uv.lock  Dockerfile  alembic.ini  migrations/
    src/refund_api/
    testing/          # test-only code
    tests/
web/                  # Next.js + TypeScript
contracts/
  refund_api.openapi.json   # exported from refund-api, committed
docker-compose.yml
.github/workflows/
docs/
```

### Boundary enforcement
1. **Import contracts (`import-linter`, run in CI):**
   - *Independence:* `agent` must not import `refund_api`, and the reverse.
   - *Core purity:* `agent.core` must not import `agent.db`, `agent.api`, `agent.worker`,
     `agent.llm`, `agent.refund_client`, or I/O libraries (`sqlalchemy`, `psycopg`,
     `httpx`, `fastapi`, `openai`). Domain logic stays pure, so the policy and capacity rules
     are unit-testable without a database and can't be bypassed by a shortcut through I/O
     code.
   - *Test code isolation:* nothing under `src/` imports from `testing/`, except the
     provider factory, behind the ADR-006 flag gate.
2. **Dependency check:** neither service's `pyproject.toml` lists the other as a
   dependency (a CI script checks this).
3. **Docker check:** each service's build context is its **own directory**, so its
   Dockerfile can't `COPY` the other service. A CI script also fails if either Dockerfile
   references the other service's path.
4. **Negative control:** a CI step adds a deliberately violating import (for example
   `agent` importing `refund_api`) to a temporary copy and asserts that `import-linter`
   **fails**. A boundary check that has never been seen to fail proves nothing (the same
   principle as ADR-004's `dedup_disabled`).

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
- Canonical-payload hashing is checked on both sides with shared **test vectors** (input
  → expected hash) stored in `contracts/`, so the duplicated implementations can't
  diverge.

### Test-only code location
- `testing/` sits **outside `src/`** in each service. It is on the Python path only in test
  runs and the CI test image. The production Docker target doesn't copy it, so
  `ScriptedLLM` and fault-injection hooks **don't exist** in a production image. That's a
  third layer on top of ADR-006's factory gate and startup check.

### Dependency management: uv
- **uv**, with an **independent `uv.lock` per service** (no uv workspace), so each service
  resolves and locks its own dependencies, the same way they'll be built.
- CI and Docker install with **`uv sync --locked`**, which fails if the lockfile doesn't
  match `pyproject.toml`, so builds always use exactly the locked versions.
- **Fallback:** if uv becomes a problem, `uv export` produces a pinned `requirements.txt`
  per service, installable with plain `pip`. Moving off uv is a mechanical change.
- Installing uv on the development machine needs the user's explicit OK (done at
  implementation time).

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

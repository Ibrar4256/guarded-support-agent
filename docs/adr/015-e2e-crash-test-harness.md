# ADR-015: The End-to-End Crash-Test Harness

**Status:** Accepted (2026-11-12). Revised the same day after outside review: services started
from their venvs' Python in their own process groups (not under `uv run`), a stop proved by
the process state, a database per test, controls that can actually fail, ADR-004's full
crash list mapped, agent data created through an agent test entry point, no row deletion,
one timing profile checked at startup, and ADR-004's test-data rule applied.

**Locked 2026-11-12.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Precedence:** narrows ADR-007's "`tests/e2e/` against `docker-compose.yml`" for
the crash tests (Part 1). Everything else in ADR-004's crash-test list, ADR-007's root CI job
and ADR-011's fault build stands.

## Context
ADR-004's crash tests are the proof that exactly-once refunds survive the dangerous window:
refund-api commits, the agent dies before recording it, a new worker resumes. The pieces now
exist: refund-api's fault build (`refund_api_testing.app:fault_app_from_env`, test routes
under `/_test`, one uvicorn worker, ADR-011) and the agent's crash-test worker
(`python -m agent_testing.worker`, which `os._exit(1)`s at a named point, ADR-014 Part 5).

Settled already: the tests live in `tests/e2e/`, run in CI's root job after both services
pass, and need both services plus Postgres (ADR-007). Open:

1. **Where the two services run** during a test, given that each test must start, kill,
   stall (`SIGSTOP`) and restart the worker with different settings, while refund-api keeps
   running.
2. **Which Python environment runs `tests/e2e/`.** ADR-007: the services share no code, and
   neither may import the other.
3. **How a test gets its data and its isolation**, when a worker claims any claimable run in
   its database and the agent's seed command doesn't exist yet.

Constraints: the development machine has about 2 GB of free disk (local image builds are out;
ADR-011's image check already runs in CI only); every wait is bounded; no test depends on a
local `.env`; a test must never pass for the wrong reason.

## Options Considered

### Part 1 — Where the services run

#### Option A: Both services as host processes, each started from its own venv's Python in its own process group; Postgres from compose — CHOSEN
- How it works:
  - A session fixture runs `uv sync --locked` once per service, then starts refund-api's fault
    build with **that service's venv Python directly**
    (`services/refund_api/.venv/bin/python -m uvicorn --factory … --workers 1`) on a free
    port. Each test starts `services/agent/.venv/bin/python -m agent_testing.worker` the same
    way. **Never under `uv run`:** uv starts Python as a child (checked: the Python process's
    parent is uv), so a signal sent to uv doesn't reach the worker. `SIGSTOP` can't be caught
    or forwarded at all.
  - Every process starts with `start_new_session=True`; signals and cleanup go to its
    **process group** (`os.killpg`), so nothing it started survives it. A fixture kills every
    group it started, whatever the test's outcome (ADR-014's process test lesson).
  - **Proof of a stop:** after `SIGSTOP`, the test asserts the worker's state is `T` in
    `/proc/<pid>/stat` before relying on it; after `SIGCONT`, that it's running again. A
    stale-worker test that never stopped anything fails here, not later.
- Pros: exact process control, which is what crash tests need (`os._exit` seen as exit code
  1, a real stall). No image builds, so it runs on this machine and in CI the same way. Each
  service runs from its own locked environment (ADR-007).
- Cons: the services aren't in containers here, so this doesn't test the images; ADR-011's
  production-image check covers refund-api's image, and the agent's arrives with its
  Dockerfile. `/proc` makes the stop check Linux-only, like CI and this machine.
- Cost/latency/complexity profile: seconds per test; one fixture module.

#### Option B: Both services in `docker-compose.yml`; tests drive them through Docker — rejected
- How it works: compose gains `refund-api-test` (fault target) and `agent-worker-test`
  services; each test runs `docker compose run -e CRASH_POINT=… agent-worker-test`.
- Pros: ADR-007's wording, literally; exercises the images.
- Cons: needs an agent Dockerfile now (it belongs to the deployment step); image builds that
  don't fit on this machine; process control through Docker (a stall means `docker pause`,
  exit codes through `compose run`); slower.
- Why we didn't use it here: it can't run locally, and it buys image coverage the image
  checks already give.
- When it WOULD be the better choice: once both images exist and the machine has room, as a
  deployment-time smoke test of the composed system (one happy path, not the crash matrix).

#### Option C: refund-api in compose (its existing test image), the worker as a host process — rejected
- How it works: compose runs the refund-api test image; the test spawns the worker locally.
- Pros: refund-api's real image in the loop; no agent image needed.
- Cons: still an image build locally (no room); two ways of running services in one test.
- Why we didn't use it here: Option A's process is the same code as the image's, without the
  build.
- When it WOULD be the better choice: CI-only crash tests with a roomy runner.

#### Option D: refund-api in-process in the test runner (uvicorn in a thread) — rejected
- How it works: the test process serves refund-api; the worker is a subprocess.
- Pros: one fewer process to manage.
- Cons: the test runner would import `refund_api`, coupling the e2e suite to one service; a
  fault in the server is a fault in the test runner.
- Why we didn't use it here: crash tests exist to keep failure domains apart.
- When it WOULD be the better choice: a single-service integration test (refund-api's own
  in-process tests already do this).

### Part 2 — The environment that runs `tests/e2e/`

#### Option A: Its own small uv project (`tests/e2e/pyproject.toml` + `uv.lock`) — CHOSEN
- How it works: black-box only: pytest, pytest-asyncio, httpx (refund-api's HTTP API and test
  routes) and psycopg (read-only SQL checks, and creating per-test databases). It imports
  neither service; it starts them, and the agent's test entry points, as processes. Locked
  like the services (ADR-007), synced in the root CI job.
- Pros: the tests can't reach into either service; independence stays checkable; the same
  lockfile discipline as everything else.
- Cons: a third lockfile; the service-independence check must allow it.
- Cost/latency/complexity profile: one `uv sync --locked` in the root job.

#### Option B: Run it from the agent's environment — rejected
- How it works: `tests/e2e` uses `services/agent`'s venv.
- Pros: no new lockfile.
- Cons: the e2e suite could import `agent` code and quietly stop being black-box; it ties
  cross-service tests to one service's dependency set.
- Why we didn't use it here: independence that isn't enforced erodes.
- When it WOULD be the better choice: a monorepo with a shared workspace (ADR-007 rejected one).

#### Option C: Ad-hoc `uv run --with pytest --with httpx …` — rejected
- How it works: dependencies named on the command line.
- Pros: nothing to maintain.
- Cons: unpinned: CI can resolve different versions on different days.
- Why we didn't use it here: the same reason every service has a lockfile.
- When it WOULD be the better choice: a one-off script.

### Part 3 — Data and isolation

#### Option A: A fresh agent database per test from a migrated template; agent data through an agent test entry point; refund-api shared, keyed by unique IDs — CHOSEN
- How it works:
  - **Isolation:** the session migrates one template database (`agent_e2e_template`, as the
    agent's owner role). Each test creates its own with
    `CREATE DATABASE agent_e2e_<id> TEMPLATE agent_e2e_template OWNER agent_owner` (as the
    Postgres superuser, which compose and CI already have), re-applies ADR-005's database-level
    grants (`REVOKE CONNECT … FROM PUBLIC`, `GRANT CONNECT` to the owner and runtime roles:
    database privileges aren't copied from a template), and passes that database's URL to
    every agent process in the test. A worker can then only ever see its own test's runs.
    refund-api stays shared: everything there is keyed by a unique `action_id`, order and key.
  - **Agent data:** `python -m agent_testing.make_approved_run` (the agent-side counterpart of
    `/_test/charges`) creates a customer, order, lookup and APPROVED run exactly as approval
    would, with the agent's own key generation and canonical payload, and prints the IDs as
    JSON. The e2e suite never writes agent rows by SQL, never tracks the agent's schema, and
    never holds a third copy of the hashing. It only reads, to assert.
  - **refund-api data:** the charge through `/_test/charges`. **ADR-004's test-data rule:**
    every charge is at least twice the refund, so a `dedup_disabled` control's second refund
    isn't refused with `refund_exceeds_charge`; otherwise those controls could never fail.
  - **No cleanup by deletion:** refund rows are immutable to refund-api's runtime role and
    `decisions` is append-only; unique IDs and a database per test make deleting unnecessary.
    Per-test databases are dropped at the end of the session.
- Pros: no cross-test claims, by construction; no dependency on the unbuilt agent seed; no
  agent schema knowledge in the e2e suite; nothing deletes what the grants protect.
- Cons: needs the superuser credentials in the e2e environment (test-only); one
  `CREATE DATABASE` per test (fast: a file copy of a small template).
- Cost/latency/complexity profile: tens of milliseconds per test database.

#### Option B: One shared agent database, tests run serially, each asserting it owns the only claimable run — rejected
- How it works: no per-test databases; a guard at the start of each test.
- Pros: no superuser use.
- Cons: a failed test's leftovers fail every later test's guard; tests can never run in
  parallel.
- Why we didn't use it here: isolation by construction beats isolation by assertion.
- When it WOULD be the better choice: environments without a superuser (a hosted database).

#### Option C: Seed both databases once from the demo manifest — rejected
- How it works: run both services' seed commands before the suite.
- Pros: the deploy path is exercised.
- Cons: the agent's half doesn't exist yet, and shared rows couple the tests to each other.
- Why we didn't use it here: not buildable yet, and each test needs fresh rows.
- When it WOULD be the better choice: a seed smoke test once the agent's half lands.

### One timing profile
Short leases and timeouts keep every wait bounded (`timeout_after_commit` costs a client
timeout plus a retry), but they must still pass every startup guard: refund-api's retention >
window + recovery delay and its processing budget; the agent's worst case ≤ the window it
accepts; lease ≥ 3 heartbeats; shutdown grace > client timeout; and, when the reconciler
arrives, its quiet period > client timeout + processing budget. The profile lives in one
module of the e2e suite, and **a test starts each service with it and asserts it came up**
(refund-api answers `GET /config`; the worker logs `claim gate open`). A guard tripped by a bad
profile fails that test, not a crash test where it would look like a crash bug.

## Decision
Part 1 A, Part 2 A, Part 3 A, and the timing profile.

### The tests here (each with a control that can fail)
| Test | Control |
|---|---|
| `before_refund_call`: the worker dies after recording the attempt, before sending; a fresh worker resumes → exactly one refund, run `COMPLETED` | **No resumed worker** → zero refunds, the run still `EXECUTING`: the refund comes from the resume, not from the crashed worker |
| `after_refund_response`: refund-api committed, the worker dies before recording; a fresh worker replays the same key → exactly one refund, run `COMPLETED` | `dedup_disabled` → **two** refunds (charge ≥ 2× refund) |
| Tight limit: 40 against a 50 per-order limit, crash after the response, resume → recorded, not blocked by a re-check | `dedup_disabled` → two (the re-check is skipped by design; ADR-004) |
| `timeout_after_commit`: the commit happens, the answer never arrives in time; the retry with the same key → one refund | `dedup_disabled` → two |
| A business-error 4xx frees capacity: a refund above its real charge → `refund_exceeds_charge` → `rejected`, `ESCALATED`, and the capacity is available to the next refund | Only real charges produce this code, so it lives here; the classification itself is unit-tested |
| Stale worker: A is stopped (`SIGSTOP`, state `T` checked) mid-run past its lease; B claims and finishes; A resumes → A's fenced writes affect 0 rows, the run shows B's result, refund-api holds one refund | **Assertion-only here.** The fencing control lives in ADR-014's in-process tier, where the query can be replaced without a production seam (a seam that disables fencing is exactly the kind refund-api guards with `deduplicates`). Adds the missing one: the attempt record without `claim_version` lets a stale worker send |
| Every test asserts its fault fired (refund-api's fault log) and a crashed worker exited with code 1 | (The controls against a vacuous pass) |

### ADR-004's crash and failure list: where each one lives
| ADR-004 test | Where |
|---|---|
| `before_refund_call`, `after_refund_response`, tight limit, `timeout_after_commit`, stale worker | Here (e2e) |
| A business-error 4xx frees capacity | Here (needs real charges) |
| Rejected at execution (policy changed after approval) | ADR-014 integration: `test_presend.py` (out of policy → `ESCALATED`, event, nothing sent) |
| Lost claim mid-retry | ADR-014 integration: `test_claim_heartbeat.py`, `test_execute.py` |
| No claiming finished runs | ADR-014 integration: `test_claims.py` |
| Escalation keeps capacity reserved (an `unknown` counts) | Policy core unit: `test_policy.py` (an EXECUTING `unknown` counts) |
| An old `unknown` still counts | Policy core unit: `test_policy.py` (`unknown` of any age) |
| Quiet period (`slow_commit`) | The reconciler (step 4) |
| ADR-010: `invalid_request` on a committed retry ends `confirmed` | The reconciler (step 4) |
| Both crash tests again under `dedup_disabled` | Here, as the controls above |

## Consequences
- New: `tests/e2e/` with its own `pyproject.toml` and `uv.lock` (pytest, pytest-asyncio,
  httpx, psycopg); `agent_testing.make_approved_run`; a root CI step after both services
  pass, with Postgres up and the superuser credentials available to it.
- `scripts/ci/check_service_independence.py` learns that `tests/e2e` is a third, black-box
  project.
- ADR-014's in-process tier gains the attempt record's fencing control.
- KNOWN_TRADEOFFS: the crash tests run the services as processes, not containers; the e2e
  suite uses the Postgres superuser to create per-test databases.
- Pointer line into ADR-007 (the e2e harness) when locked.
- **Revisit if:** the machine gets room for images, or the deployment step adds the agent
  image (then Part 1 B as a composed smoke test).

## Interview-ready summary
"My crash tests run both services as real processes: refund-api's fault build stays up while
the test kills the agent's worker at a named point with `os._exit`, so no cleanup code runs,
then starts a fresh worker and checks refund-api holds exactly one refund. The control turns
deduplication off and the same crash then produces two, which only works because every charge
is at least twice the refund, or the provider's own over-refund check would hide the double.
A review caught that my first harness would have lied twice: `uv run` sits between the test
and the worker, so stopping it wouldn't stop the worker, and one of my controls couldn't fail.
Now each service runs from its own venv in its own process group, every stop is confirmed in
`/proc`, every control breaks the property it guards, and each test gets its own database so
no worker can claim another test's run."

# Guarded Support Agent

A customer-support agent that **takes real actions** (looks up orders, issues refunds
within policy, escalates to a human) with safety enforced in code, not in the prompt.
It's a scoped-down, architecturally real take on per-resolution support agents like
Intercom Fin and Ada.

> **Status: the money path and the first tool are built; the model loop is next.** Twenty-two
> architecture decisions are recorded below. Built and tested in CI: the refund service, the
> agent's policy core, database and refund client, the worker (sending and reconciliation,
> with end-to-end crash tests), the demo seeds, and the order-lookup tool. Not built yet: the
> LLM loop, the chat API and UI. There are no eval numbers yet, and none will appear here
> until they're measured.

## Core idea: the model proposes, code decides

```
AGENT_STEP ──(POLICY_CHECK)──┬─ in policy ──────────────→ APPROVED
     │                       ├─ high risk → AWAIT_APPROVAL → APPROVED (reviewer)
     │                       └─ out of policy ─────────→ ESCALATED
     └─ answer, no proposal ─→ COMPLETED
APPROVED → EXECUTING → COMPLETED | ESCALATED | NEEDS_RECONCILIATION → COMPLETED | ESCALATED

AGENT_STEP runs a bounded tool loop: read tools run freely; write tools
(refund, escalate) only return a proposal. Full lifecycle: ADR-008.
```

- **Write tools never execute inside the model loop.** A refund is a proposal that passes
  a code-only policy check and, when high risk, a human reviewer.
- **Identity comes from the session, never from model-filled arguments.** All tool output
  (KB articles, order notes, past tickets) is treated as untrusted input.
- **Exactly-once refunds across crashes:** idempotency keys saved before the call, a
  separate refund service that deduplicates, and crash tests on both sides of the call
  with a negative control that must fail.
- **Once a refund is approved, the model is out of the loop.** Even the confirmation
  message is a template filled in from the database, so the agent can't claim a refund
  happened when its outcome is unknown.

### Scoped safety claim
Prompt injection cannot make an **out-of-policy** action, or an action on **another
user's** resources, execute without human approval. Two things are *not* claimed and are
measured instead: in-policy refunds triggered by injection, and data leaking through read
tools.

## Architecture decisions

| ADR | Decision |
|---|---|
| [001](docs/adr/001-agent-loop-architecture.md) | Hybrid loop: deterministic state machine around a bounded tool loop (vs. LangGraph, a native loop, Temporal, agent SDKs) |
| [002](docs/adr/002-conversation-and-user-memory.md) | Memory: sliding window + rolling summary; past-ticket digest built by code. Memory never authorizes anything |
| [003](docs/adr/003-auth-and-pending-review-flow.md) | Clerk auth verified in FastAPI; pending-review flow over a resumable event stream |
| [004](docs/adr/004-refund-api-placement-and-idempotency.md) | Separate refund service, idempotency protocol, failure injection, crash tests |
| [005](docs/adr/005-database-access-and-migrations.md) | SQLAlchemy Core (async) + Alembic; a per-customer row lock against write skew |
| [006](docs/adr/006-scripted-model-stub.md) | Strict scripted model stub for deterministic tests; nightly real-model drift check |
| [007](docs/adr/007-repo-layout.md) | Monorepo of independent services with no shared code; boundaries enforced by import contracts and an HTTP contract test; uv lockfiles |
| [008](docs/adr/008-run-lifecycle-and-cross-adr-clarifications.md) | Seven run statuses with database-enforced invariants; a reconciler that never re-sends; cross-ADR clarifications |
| [009](docs/adr/009-refund-policy-rules-and-decision-audit.md) | Refund policy as pure rules with stable IDs (split-refund, re-ask and queue-flooding defenses), hash- and revision-bound approvals, breaker and kill switch, append-only decision log |
| [010](docs/adr/010-refund-api-implementation-decisions.md) | refund-api implementation: validation errors are "unknown" to the agent, charges locked per order, fault plans that must prove they fired, advisory-lock 409s, key pruning with an owner-set floor, bearer service tokens, a deadline counted from receipt |
| [011](docs/adr/011-refund-api-test-build-wiring.md) | refund-api's fault-injection build is a separate entry point in the test package that wraps the production app, so production code never imports test code |
| [012](docs/adr/012-refund-client-classification-and-contract.md) | The agent's refund client: a pure, strict, property-tested response classifier; a JSON Schema contract test against the committed spec; retry limits that hold across crashes and config changes (a per-send age check with a per-run window snapshot, a persisted attempt count, per-key expiry in refund-api); `GET /config` gating only the worker's send path |
| [013](docs/adr/013-provider-config-read-before-each-send.md) | The provider's config is read, strictly validated and capped immediately before every send, so a retention decrease can't leave the agent retrying under a stale window |
| [014](docs/adr/014-worker-execution-loop.md) | The worker: a separate process that claims approved runs with a lease and fencing, spends a persisted attempt budget, and stops claiming while the provider's config can't be read |
| [015](docs/adr/015-e2e-crash-test-harness.md) | End-to-end crash tests: both services as real processes, a per-test database, crashes on both sides of the refund call, each proving its fault fired |
| [016](docs/adr/016-reconciler.md) | The reconciler: waits out a quiet period that covers any request still in flight, confirms or rejects by lookup, never re-sends; tells a provider outage from a broken run |
| [017](docs/adr/017-reconciler-streak-evidence.md) | A failed lookup counts against the run only after a successful lookup in between, not a config read; a stale-unknown page |
| [018](docs/adr/018-provider-circuit-breaker.md) | Provider breakers, one per path, judged on real request outcomes: an outage pauses sending instead of escalating every refund |
| [019](docs/adr/019-demo-seed-lifecycle.md) | The agent's demo seed: refuses outside a demo, refuses changed facts, refreshes state, dates relative to today |
| [020](docs/adr/020-order-lookup-tool.md) | The order-lookup tool: identity from the session, ownership in the query, byte-identical "not found" for missing and foreign orders, a security log fed by a probe that can't change the answer |
| [021](docs/adr/021-refund-api-floor-and-demo-seed-split.md) | refund-api's key-retention floor set on every deployment by its own command and checked at startup; demo charges only on a demo |
| [022](docs/adr/022-agent-turn.md) | The agent turn: the conversation lives in the event log and tool results are never replayed; call, malformed and time budgets; a provider failure means "try again", not escalation; untrusted text in a per-turn nonce fence; a proposal on an order not looked up in this run is sent back before policy |

Every ADR records the rejected options and when each would have been the better choice.

## Docs
- [Project brief](docs/PROJECT_BRIEF.md): scope, design requirements, timeline
- [Eval plan](docs/EVAL_PLAN.md): test suites, adversarial categories, reporting rules
- [Known tradeoffs](KNOWN_TRADEOFFS.md): what's deferred or deliberately not solved
- [Weekly log](WEEKLY_LOG.md): what shipped, what broke, what was learned
- [Token rotation runbook](docs/ops/refund-api-token-rotation.md): rotating refund-api's
  service token without locking the agent out

## Running the checks

Needs [uv](https://docs.astral.sh/uv/) 0.12.23 and Docker. The same script runs in CI.

```bash
scripts/ci/write_test_env.sh            # creates .env with throwaway passwords
docker compose up -d --wait postgres    # two databases, owner/runtime roles
set -a; . ./.env; set +a
scripts/ci/service_checks.sh agent      # lint, types, purity, contracts, migrations, tests
scripts/ci/service_checks.sh refund_api
```

### Every deployment: refund-api's key-retention floor

After migrations and before starting refund-api, every deployment (demo or not) sets refund-api's
key-retention floor from `REFUND_API_KEY_RETENTION_SECONDS`, as the owner role (ADR-010, ADR-021).
refund-api refuses to start without it, with a message naming this command.

```bash
(cd services/refund_api && uv run python -m refund_api.retention_floor)
```

### Seeding the demo

One manifest, [`infra/seed/demo_manifest.json`](infra/seed/demo_manifest.json), feeds both
services' seed commands, each as its own owner role (ADR-010, ADR-019, ADR-021). Run both at every
demo deploy, after the floor and before starting the worker: re-running refreshes delivery dates
(`delivered_days_ago`), so demo orders stay inside the refund window. Both seeds run only with
`DEMO_SEED_ENABLED=true` (otherwise exit 2, nothing written). The agent's seed leaves orders with an
unfinished run alone (and says which), and never touches orders the manifest doesn't list.

```bash
(cd services/agent && uv run python -m agent.seed ../../infra/seed/demo_manifest.json)          # customers + orders
(cd services/refund_api && uv run python -m refund_api.seed ../../infra/seed/demo_manifest.json) # charges
```

### refund-api by hand

```bash
cd services/refund_api
uv run alembic upgrade head                                  # as the owner role
uv run python -m refund_api.retention_floor                   # every deployment
uv run python -m refund_api.seed ../../infra/seed/demo_manifest.json   # demo only (DEMO_SEED_ENABLED=true)
uv run uvicorn --factory refund_api.app:app_from_env         # production build
DEBUG_FAULT_INJECTION=true uv run uvicorn --factory \
  refund_api_testing.app:fault_app_from_env --workers 1      # fault-injection build
```

The wire contract is [`contracts/refund_api.openapi.json`](contracts/refund_api.openapi.json);
regenerate it with `uv run python scripts/export_openapi.py` (CI fails if it's stale).

Without the database variables, integration tests are skipped locally (unit tests still run);
in CI a missing variable fails the run.

## Planned stack
Async FastAPI · Postgres (SQLAlchemy Core, Alembic, psycopg 3) · Clerk · Next.js +
TypeScript · free-tier Groq/Gemini behind a provider-agnostic interface · Docker Compose ·
GitHub Actions

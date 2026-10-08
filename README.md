# Guarded Support Agent

A customer-support agent that **takes real actions** (looks up orders, issues refunds
within policy, escalates to a human) with safety enforced in code, not in the prompt.
It's a scoped-down, architecturally real take on per-resolution support agents like
Intercom Fin and Ada.

> **Status: design complete, implementation starting.** Eight architecture decisions are
> recorded below. No code yet. There are no eval numbers yet, and none will appear here
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

Every ADR records the rejected options and when each would have been the better choice.

## Docs
- [Project brief](docs/PROJECT_BRIEF.md): scope, design requirements, timeline
- [Eval plan](docs/EVAL_PLAN.md): test suites, adversarial categories, reporting rules
- [Known tradeoffs](KNOWN_TRADEOFFS.md): what's deferred or deliberately not solved
- [Weekly log](WEEKLY_LOG.md): what shipped, what broke, what was learned

## Planned stack
Async FastAPI · Postgres (SQLAlchemy Core, Alembic, psycopg 3) · Clerk · Next.js +
TypeScript · free-tier Groq/Gemini behind a provider-agnostic interface · Docker Compose ·
GitHub Actions

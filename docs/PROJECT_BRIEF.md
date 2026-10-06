# Guarded Support Agent — Project Brief

Technical scope for Project 2. Decisions are recorded in `docs/adr/`; the eval plan is in
`docs/EVAL_PLAN.md`.

## Problem

Support-automation products (Intercom Fin, Ada) charge per resolution for agents that
*act*: they look up orders, issue refunds within policy and escalate to humans. This
project builds an architecturally real version of one, with safety enforced in code and
measured, not assumed.

## Architecture (ADR-001)

**Option D: a deterministic state machine around a bounded tool loop.**

```
INTAKE → AGENT_STEP → POLICY_CHECK → AWAIT_APPROVAL → EXECUTE → RESPOND / ESCALATE
```

- The state machine owns policy, human review, escalation and the run state saved in
  Postgres (`runs` table).
- `AGENT_STEP` runs a provider-agnostic tool-calling loop with a step budget.
- **Read tools** (order lookup, KB search) run inside the loop.
- **Write tools** (refund, escalate) never execute inside the loop. They return a
  *proposed action* that leaves the loop and goes through `POLICY_CHECK` and, if high
  risk, `AWAIT_APPROVAL`.
- A naive native tool loop (Option C) is built as the **red-team baseline**: the same
  adversarial suite runs against both.

### Safety claim (scoped precisely)

> Prompt injection cannot cause an **out-of-policy** action, or an action on **another
> user's** resources, to execute without human approval.

What this does **not** claim:
- An injected request can still produce an **in-policy** proposal (for example a small
  refund on an eligible order) that auto-executes. This is measured, not assumed away.
- Gating write tools does not prevent **data leaks through read tools**: injected text
  can try to make the agent repeat KB or order content. This is measured as its own
  category.

## Design requirements

1. **All tool output is untrusted.** KB articles, order notes, ticket text and past
   tickets can contain injected instructions, so tool output is treated as data, never as
   instructions.
2. **Identity is injected by code.** `user_id` and roles come from the authenticated
   session. They are never taken from arguments the model fills in. Order lookup checks
   that the order belongs to the session user.
3. **Pydantic validation.** Tool schemas and proposed actions are Pydantic models. A
   malformed tool call is retried a bounded number of times, then escalated.
4. **Step budget** on the tool loop. At most one write-tool call per turn; once a run is
   `APPROVED`, the model is never called again, and customer replies are templates
   filled in from database state (ADR-001, ADR-006).
5. **End-to-end idempotency.** Every proposed action carries an idempotency key, and the
   **mock refund API itself deduplicates on that key** (Stripe `Idempotency-Key`
   semantics). Exactly-once comes from the downstream API deduplicating; the `runs` table
   alone can't know whether a call made before a crash succeeded. refund-api is a separate
   service with its own database, behind a `RefundClient` interface. The key is created
   at proposal time and saved before the call. A timeout means unknown, not failed, and
   gets retried with the same key. Full protocol in ADR-004.
6. **Re-check policy once, before the first send.** An approved action is checked again
   when it moves to `EXECUTING` (the order may have changed while it waited), in the same
   transaction as a per-customer row lock (ADR-005). It's never re-checked on resume
   (ADR-004 step 6).
7. **Memory** (ADR-002): short-term conversation memory plus long-term user context
   (past tickets). Memory content is untrusted input, too.
8. **Auth and the pending-review flow** (ADR-003): Clerk handles sign-in; FastAPI verifies
   every token (JWKS signature, exp/nbf, azp) and enforces customer and reviewer roles.
   The chat stream ends cleanly when a run is waiting for approval, and the customer
   receives the outcome later.
9. **Reuse from Project 1.** The provider, retry and cost/latency telemetry pieces of the
   Research Copilot's LLM layer are extracted and ported to async (ADR-005). The
   KB-search tool is a thin wrapper over existing retrieval work, not new RAG.

## Product surfaces

Next.js + TypeScript frontend:
- **Customer chat:** SSE streaming, visible tool-status updates, and a "pending review"
  state.
- **Reviewer console:** an approval queue showing the proposed action, the customer's
  message and the policy-check result, with approve and reject buttons.
- **Trace viewer:** every step of a run with tool inputs and outputs, latency, tokens and
  cost.

Backend: async FastAPI, Postgres via SQLAlchemy Core + psycopg 3 with Alembic migrations
(Neon; ADR-005), Clerk auth with customer and reviewer roles, rate limiting, input
validation, Docker, GitHub Actions CI, and a live deployment with synthetic data.

## Evaluation (summary — full plan in `docs/EVAL_PLAN.md`)

- Adversarial suite of 20–30 cases: direct injection; indirect injection through ticket
  text, KB articles, order notes and past tickets; cross-user order IDs; multi-turn
  pressure ("my manager approved this"); refund-limit boundary pushes; read-tool data
  exfiltration.
- Run against the baseline (C) and the hybrid (D), with each case run k=3 times. Results
  are reported as **counts** (for example `0/28 × 3`), not percentages from small samples.
- Also measured: resolution rate, correct-escalation rate, policy violations, cost per
  ticket, p50/p95 latency, and a second model if time allows.
- **Crash tests (ADR-004):** kill the agent worker before the refund call and, separately,
  after refund-api commits but before the result is recorded; resume and assert exactly
  one refund each time. A negative control with deduplication disabled must show a double
  refund, which proves the test can fail. refund-api's failure-injection modes (500 after
  commit, timeout after commit) are tested the same way.
- CI: deterministic tests plus an LLM smoke set on every PR; the full adversarial suite
  nightly or on a PR label (free-tier quota and nondeterminism make per-PR full runs
  flaky).
- Only measured numbers are reported.

## Scope

Out of scope: multi-agent setups, voice, new RAG work.

| Days | Focus |
|------|-------|
| 1-4 | State machine, tools split by effect, policy checks, runs table, idempotent mock refund API, tracing |
| 5-8 | Auth and roles, streaming API, pending-review flow, chat UI, approval queue |
| 9-11 | Trace viewer, adversarial suite, baseline comparison, eval table |
| 12-14 | Deployment, CI, demo GIF, README with architecture diagram |

Days 5-8 are the schedule risk (auth alone can take 2 days). If time runs short, cut in
this order: the LangGraph port spike, then the second-model comparison, then UI polish.

**Optional spike:** a timeboxed port of the outer state machine to LangGraph, comparing
lines of code and what `interrupt()` actually saves.

## Definition of done

The README shows: a live demo link, a GIF of chat → proposal → reviewer approval →
completion, the state-machine diagram, the baseline-vs-hybrid results table and a
design-decisions section linking the ADRs. The CLAUDE.md §7 engineering bar applies.

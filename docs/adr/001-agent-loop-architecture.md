# ADR-001: Agent Loop Architecture

**Status:** Accepted (2026-10-01). Revised 2026-10-06 (from ADR-006): model use ends at
`APPROVED`, so customer replies after approval are templates; at most one write-tool call
per turn. Revised 2026-10-08: the canonical run statuses and transitions are defined in
[ADR-008](008-run-lifecycle-and-cross-adr-clarifications.md) (seven saved statuses;
`INTAKE`, `POLICY_CHECK` and `RESPOND` are steps inside transactions).

**Locked 2026-10-08.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one. Where this ADR and ADR-008
disagree, ADR-008 wins.

## Context
The agent takes real actions on a customer's behalf: order lookup, refunds within policy
limits, KB search and escalation to a human. Two constraints drive the decision:

1. **The LLM must never be what enforces policy.** Refund limits, order ownership and
   "escalate on legal threats" must be code the model can't skip, argue its way past or be
   injected out of. The real question is where control flow lives: in the model, in a
   framework's graph, or in our code.
2. **Human review needs durable, resumable state.** A high-risk action may wait in a
   review queue for hours and then resume in a different process.

Other constraints: one developer, about 2 weeks, free-tier LLMs (Groq/Gemini) behind a
provider-agnostic interface, and CLAUDE.md §3's requirement to build an agent loop by
hand at least once. Project 1 (Hybrid RAG Copilot ADR-005) already hand-rolled a
single-tool loop, so this project needs to go further: multiple tools, side effects and
pause/resume.

## Options Considered

### Option D: Hybrid — deterministic state machine around a bounded tool loop — CHOSEN
- How it works: An explicit state machine (`INTAKE → AGENT_STEP → POLICY_CHECK →
  AWAIT_APPROVAL → EXECUTE → RESPOND / ESCALATE`; the exact saved statuses and
  transitions are in ADR-008) is saved as a row in a Postgres `runs` table. Inside `AGENT_STEP`, a provider-agnostic tool-calling loop runs with a step
  budget. Tools are split by effect. Read tools (order lookup, KB search) execute inside
  the loop. Write tools (refund, escalate) return a validated *proposed action* that
  leaves the loop and goes through `POLICY_CHECK` and, if high risk, `AWAIT_APPROVAL`.
- Pros: The model reasons freely but has no path to execute side effects. Policy lives in
  a few small, testable, code-only states. Human review is a status value plus resume by
  `run_id`. Everything is debuggable in our own code. Our own trace spans feed Project 5
  directly. It exercises both the tool-use protocol and durable execution.
- Cons: The most design work up front, because the read/write line must be drawn
  correctly. Two layers to test. We own saving, resuming, retries and idempotency.
- Cost/latency/complexity profile: No framework overhead; cost is set by loop shape (LLM
  calls per ticket and history resent per call). `INTAKE` can route pure FAQ tickets down
  a one-call path, a cheap cost lever. Medium complexity, roughly a few hundred lines for
  the orchestrator.

### Option A: LangGraph — rejected
- How it works: A graph of nodes and edges with typed state. A checkpointer saves state
  after every step, and `interrupt()` pauses a run until a human resumes it.
- Pros: Pause/resume for human review is built in, which fits the review queue well.
  Checkpoint history helps debugging. Widely recognized, with integrations for
  Langfuse/LangSmith.
- Cons: Frequent API churn between versions. Stack traces run through framework code.
  LangChain message types leak into domain code. Tool calling on Groq/Gemini goes through
  LangChain adapters, which add another layer of quirks. Saved checkpoints can stop
  loading after an upgrade.
- Why we didn't use it here: It hides exactly the durable-execution and idempotency
  mechanics this project exists to demonstrate, and with fewer than 8 states the graph
  abstraction doesn't pay for itself.
- When it WOULD be the better choice: A team shipping several agents with shared
  infrastructure; graphs with parallel branches, sub-agents, or more than about 8 states;
  or when time to first working human-review flow matters more than owning the
  mechanics.

### Option B: Pure hand-rolled state machine (no inner tool loop) — rejected
- How it works: Every step is an explicit state, and the LLM is called for single
  decisions (classify, extract parameters, write a reply) instead of running a free tool
  loop.
- Pros: The most predictable and auditable option; every LLM call has one narrow job.
- Cons: Rigid. Multi-step lookups (find the order, then check the KB policy, then decide)
  need a new state for every path. It doesn't exercise real tool-calling behavior,
  including the failures (malformed arguments, invented IDs) we need to test against.
- Why we didn't use it here: D keeps B's deterministic shell and adds a bounded loop
  where flexibility is actually needed, so B is subsumed.
- When it WOULD be the better choice: Heavily regulated flows (banking, healthcare) where
  every LLM decision must be individually auditable and the set of intents is small and
  fixed.

### Option C: Native tool-calling loop — rejected as the design, kept as baseline
- How it works: `while the model wants a tool: run it, append the result, call again`.
  This can be a provider SDK runner or about 50 lines over the OpenAI-compatible
  format.
- Pros: The least code, and the closest to the protocol. The model gets the most
  freedom.
- Cons: The model decides control flow. Guardrails can only live inside tool code, with
  no state the model is forced through. Pausing mid-loop for human review means
  serializing the message list by hand. Vendor SDKs create lock-in. Infinite loops and
  invented order IDs are both possible.
- Why we didn't use it here: Policy enforcement would depend on every tool implementer
  getting checks right, with no structural guarantee.
- When it WOULD be the better choice: Read-only assistants (search, Q&A) where no tool has
  side effects, and fast prototypes. **We build it anyway as the naive baseline** that the
  adversarial suite attacks, which gives a measured comparison against D.

### Option E: Durable-execution engine (Temporal) — rejected
- How it works: The agent run is a Temporal workflow and each tool call is an activity.
  The engine persists history, retries activities and resumes workflows after crashes or
  long waits.
- Pros: Industrial-grade durability, timers (for review timeouts) and retries come
  built in.
- Cons: A separate server cluster plus workers to operate. Activities are still
  at-least-once, so side effects need idempotency keys anyway. It's a heavy learning
  curve for a 2-week build.
- Why we didn't use it here: A Postgres `runs` table with idempotency keys gives the same
  guarantees at this scale, and Temporal doesn't remove the idempotency work.
- When it WOULD be the better choice: Long-running, multi-step workflows across many
  services at production volume, where a team can operate the cluster.

### Option F: Lightweight agent frameworks (PydanticAI, OpenAI Agents SDK) — rejected
- How it works: Typed agent abstractions with tool decorators, structured outputs and
  built-in tracing hooks wrapped around the native loop.
- Pros: Less boilerplate than hand-rolling C. Strong typing (PydanticAI). Built-in tracing
  and guardrail hooks (OpenAI Agents SDK).
- Cons: Still a model-driven loop at the core, so the policy problem is the same as C's.
  They add another dependency and abstraction over the tool-use protocol. Support for
  non-OpenAI providers varies.
- Why we didn't use it here: They solve the boilerplate problem, not the control-flow
  problem. We use plain Pydantic for schemas and validation, which gives the typing
  benefit without the framework.
- When it WOULD be the better choice: Product teams building model-driven assistants
  quickly, especially on a single provider, where policy-critical side effects are few
  or absent.

## Decision
We chose D because it is the only option where **"the model proposes, code decides" holds
by structure**, not by convention: write tools can't execute inside the loop, so policy
enforcement doesn't depend on prompt compliance or on every tool implementer adding
checks. At fewer than 8 states, a hand-rolled shell plus a `runs` table is smaller than
the framework it replaces. It also satisfies the CLAUDE.md requirement to build an agent
loop by hand and gives our own trace spans for Project 5.

## Consequences

**Safety claim, scoped precisely.** D guarantees that prompt injection cannot cause an
**out-of-policy** action, or an action on **another user's** resources, to execute
without human approval. It does **not** guarantee:
- that an injected request can't produce an **in-policy** proposal that auto-executes
  (for example a small refund on an eligible order). We measure this rate as its own eval
  category instead of claiming zero.
- that **read tools can't leak data**. Injected text can try to make the agent repeat KB
  or order content in its reply, and the write gate doesn't cover this. It's measured as
  its own category.

**Exactly-once execution requires the downstream API to deduplicate.** If the process
dies after the refund call and before the result is written, the `runs` table can't know
whether the refund went through. Every proposed action therefore carries an idempotency
key, and the **mock refund API persists and deduplicates on it** (Stripe
`Idempotency-Key` semantics). The crash test kills the process in exactly that window and
asserts one refund. A negative control with deduplication disabled must show two refunds,
which proves the test can fail.

**Evals in CI are split by determinism.** Free-tier quota limits and model nondeterminism
make a full LLM suite on every PR produce flaky red builds (the same issue Project 1
ADR-011 hit). So:
- On every PR: deterministic tests (policy checks, state transitions, schema validation,
  the crash test with a scripted model stub) as the merge gate, plus a small real-model
  smoke set that reports but never blocks (ADR-008).
- Nightly or on a PR label: the full adversarial suite, with each case run k=3 times.
- Results are reported as counts (`0/28 × 3`), not percentages: with n≈28, "0% attack
  success" is weak statistical evidence and is presented that way.

**Model use ends at the proposal (added 2026-10-06, ADR-006).** Once a run is `APPROVED`,
no step calls the model. The execution-time policy check, the refund call, retries,
reconciliation and the customer reply are all code. `RESPOND` after approval fills
**templates** from database state (`refund_outcome`, amount, order, escalation), so the
agent can never tell a customer "refund issued" while the outcome is `unknown`. The model
writes customer text only before a proposal (answers, clarifying questions, FAQ replies).

**One proposal per run (added 2026-10-06).** Within a turn, all read-tool calls run in
order. A turn with more than one write-tool call, or a write-tool call mixed with other
calls, is malformed: nothing in it executes, the model gets an error and a bounded retry,
then the run escalates.

**What we give up:** LangGraph's built-in `interrupt()` and checkpoint history, Temporal's
timers and retries, and framework tracing. We write and maintain saving, resuming,
review-timeout handling and tracing ourselves.

**Revisit if:** the state machine grows past about 8 states, needs parallel branches or
sub-agents, or the hand-rolled persistence becomes the main source of bugs. The optional
LangGraph port spike will measure what migrating would actually cost and save.

## Interview-ready summary
"I chose a hybrid: a deterministic state machine wrapping a bounded tool-calling loop.
Read tools run freely, but write tools like refunds can only *propose* an action, which
code checks against policy and routes to human approval. I didn't use LangGraph because
its `interrupt()` and checkpointer would have hidden the durable-execution and
idempotency work I wanted to own, and at fewer than 8 states the graph doesn't pay for
itself. LangGraph would be the better call with many agents, parallel branches or a team
sharing infrastructure. A plain native loop is what most quick demos use, and I built
one deliberately as the baseline to attack, so I could measure the safety difference
instead of asserting it. Temporal would win at production scale across services, but its
activities are still at-least-once, so you need idempotency keys either way."

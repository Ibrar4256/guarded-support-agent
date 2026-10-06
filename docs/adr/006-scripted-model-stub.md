# ADR-006: Scripted Model Stub for Deterministic Tests

**Status:** Accepted (2026-10-06). Revised the same day before locking: model use ends at
`APPROVED` (templated replies after that), a two-layer production gate with a test,
matcher rules, a minimum set of contract scenarios, real-provider fixtures, explicit token
counts, streaming, and baseline compatibility.

**Locked 2026-10-06.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

## Context
Every test that runs on every PR (crash tests, races, failure modes, policy checks,
ADR-003 to ADR-005) drives the agent loop. Those tests need a model that:
- behaves **the same way every time** (no flaky merge gates; see Project 1 ADR-011),
- costs nothing and needs no network or secrets in CI,
- can produce **specific model failures on demand** (malformed tool arguments, unknown
  tools, rate limits, timeouts), which real models produce only occasionally.

The real LLM layer is Project 1's provider, retry and telemetry code, ported to
`AsyncOpenAI` (ADR-005), and it talks to Groq/Gemini over the OpenAI-compatible API.

## Options Considered

### Option A: Scripted fake provider behind the provider interface — CHOSEN
- How it works: `ScriptedLLM` implements the same async provider interface as the real
  client and plays back scripted **turns**: a tool call, text, malformed arguments, an
  unknown tool name, an empty reply, or a raised error (rate limit, timeout). It records
  every prompt it receives.
- Pros: Fully deterministic, free and fast. Any model failure can be scripted at any
  step. Prompt recording lets tests check what the model was *shown* (trust boundaries,
  schema contents). The baseline loop (ADR-001 Option C) runs on the same scripts, so the
  baseline-vs-hybrid comparison uses identical inputs.
- Cons: It proves the loop handles the behavior *we scripted*, not what real models do
  (see Consequences: drift). The scripts are code to maintain.
- Cost/latency/complexity profile: $0; milliseconds per test; about 200 lines plus
  fixtures.

### Option B: Record and replay (vcrpy / pytest-recording) — rejected
- How it works: Real API responses are recorded once and replayed, matched by request.
- Pros: Realistic payloads, with no hand-written responses.
- Cons: Recordings break on any prompt change (the request body changes). It can only
  replay failures that actually happened, so "malformed tool call on step 3" can't be
  produced on demand. Recordings go stale silently.
- Why we didn't use it here: Failure injection is the main point, and recordings can't
  provide it.
- When it WOULD be the better choice: Stable integrations with fixed prompts, where
  realistic payload shapes matter more than injecting failures.

### Option C: Small local model (Ollama) in CI — rejected for gating
- How it works: A small open model serves the OpenAI-compatible API inside CI.
- Pros: Real tool-calling behavior, including real mistakes.
- Cons: Not deterministic even at temperature 0 across versions and hardware. Slow, with
  a heavy CI image. It still can't produce a specific failure on demand.
- Why we didn't use it here: Merge gates must be deterministic.
- When it WOULD be the better choice: A cheap smoke-test layer when no hosted free tier
  is available.

### Option D: HTTP-level mock of the OpenAI-compatible API (respx) — used narrowly
- How it works: Fakes the wire format under the real async client.
- Pros: Exercises the real adapter: request building, response parsing, 429 handling,
  retries, and timeout mapping.
- Cons: Scripts are wire-format JSON, which is verbose for multi-turn loop tests.
- Decision: **used only for adapter and contract tests** (below), not for loop tests.

### Option E: Patching the client with unittest.mock — rejected
- How it works: `patch("...client.chat.completions.create")` returns canned objects.
- Pros: No new code.
- Cons: Couples tests to internal call paths, so refactors break tests. Easy to return
  objects the real SDK never produces. No strictness or prompt recording.
- Why we didn't use it here: It tests the implementation, not the contract.
- When it WOULD be the better choice: One-off unit tests of a small helper.

## Decision
**A for every loop, state-machine and safety test, plus D for a small set of adapter and
contract tests.**

### Rules for ScriptedLLM
1. **Strict playback.** An unexpected call, or a run that ends with unused turns, fails
   the test. There's no default response that would let a broken loop pass. **Failure
   messages** report: which call was unexpected (call number, run ID), a structural
   summary of that prompt (message roles, tool names offered, last user message), and the
   list of unused turns.
2. **Matcher-based turns.** A turn is either positional (the next call) or matched, so a
   legitimate change in loop order doesn't break scripts. Rules:
   - Matchers key on **structure**: message roles, message count, tool names offered,
     tool results present. A matcher on message **text** is allowed only for text the
     test itself injected (for example a planted KB article). Matching on our own prompt
     wording would break on every prompt edit, which is Option B's weakness again.
   - If **more than one** turn matches a call, the test errors; ambiguous scripts aren't
     allowed.
   - Turns are consumed **in order of arrival**: each call takes the first eligible turn,
     which is then used up unless it's explicitly marked `repeat`. A turn that never
     matches fails the test at the end as "unused".
3. **Prompt recording with structural assertions.** Tests assert structural facts, never
   full prompt strings: the untrusted-data block exists and contains the KB text; no tool
   schema has a `user_id` parameter; the past-ticket digest has only the allowed fields
   (ADR-002). Exact-string assertions would recreate Option B's brittleness.
4. **Fixed token counts per turn**, so cost and latency telemetry are tested
   deterministically. ★ There is **no silent default**: a turn without counts records
   "uncounted", and the cost-assertion helper **refuses to run** on a run with any
   uncounted turn. A default of 0 would make cost assertions pass by accident.
5. **Deliberate bad outputs** in the standard script library: malformed JSON arguments,
   an unknown tool name, a write-tool call missing a required field, an empty reply, a tool
   call naming another user's order, **several tool calls in one turn**. These are the
   model failures the loop must survive (ADR-001 design requirements 3 and 4).
   - **Naming convention:** scripts live in `tests/scripts/<category>/<name>.yaml`
     (categories: `happy`, `bad`, `attack`, `crash`, `race`), and each has `represents:`
     (the real failure it stands for) and `source:` (`synthetic` or `observed:<run-id>`).
     The eval plan and the weekly log refer to scripts by that path, so they can be found
     with grep.
6. ★ **Several tool calls in one turn** (common with real providers): all **read-tool**
   calls in the turn run, in order. A turn with **more than one write-tool call**, or a
   write-tool call **mixed with other calls**, is treated as malformed: nothing in it
   executes, the model gets an error message and a bounded retry, then the run escalates.
   One run produces at most one proposal.

### Streaming (in scope)
The real adapter streams text tokens to the customer's SSE channel (ADR-003) during
`AGENT_STEP` and the FAQ path. `ScriptedLLM` text turns therefore **emit chunks** (chunk
boundaries are configurable, including a single chunk and one chunk per character), so the
streaming path, including reassembly and the "tool status" events between chunks, is tested
on every PR.

### Baseline loop compatibility
The naive baseline loop (ADR-001 Option C) uses the **same script format**. Scenarios
shared by both loops (the adversarial comparison) use **matcher-based turns keyed on tool
names**, because the two loops make different numbers of calls. Positional turns are
allowed only in tests specific to one loop.

### Concurrency (ADR-005 race tests)
Each run gets **its own script instance**, looked up by `run_id` in a test registry, so
parallel runs never use up each other's turns.

### Crash tests across a process boundary (ADR-004)
- **A fresh script per phase, not a saved cursor.** The worker subprocess loads its script
  from a file given by an environment variable. Phase 1 (before the crash) gets the
  pre-crash script. Phase 2 (the resumed worker) gets its own script.
- ★ **Model use ends at the proposal.** Once a run is `APPROVED` (by policy or by a
  reviewer), **no step calls the model**. Everything after that (the execution-time policy
  check, the refund call, lock-timeout retries, reconciliation and the customer reply) is
  code. **Customer replies after `APPROVED` are templates** filled in from database state
  (`refund_outcome`, amount, order, escalation), never model-generated. This is a decision,
  not just a test convenience: a model-written reply could say "refund issued" while the
  outcome is `unknown`, which is a hallucinated financial claim. ADR-001's `RESPOND` state
  is therefore code-only after approval.
- **So any run resumed in `APPROVED` or later gets an empty phase-2 script.** In strict
  mode, that asserts **resuming makes zero model calls**. This covers both the
  `before_refund_call` crash (still `APPROVED`) and the `after_refund_response` crash
  (`EXECUTING`). A model call on resume could produce a different proposal or a reply that
  contradicts the recorded outcome.

### Time
- Timeouts and rate limits are simulated by **raising** the real timeout and rate-limit
  error types, never by sleeping. In-process timing uses an injectable clock that tests
  replace with a fake one.
- ★ A fake clock can't fake Postgres's `now()`. Tests that depend on database time (lease
  expiry, review deadlines, the reconciliation quiet period) use **short real config
  values** (for example a 1-second lease) instead.

### Contract test: the stub and the real adapter must agree
The same scenarios run through **both** `ScriptedLLM` and the real adapter, the latter with
D-level `respx` mocks. **Minimum scenario set** (it may grow, never shrink): a single tool
call, plain text, a streamed text reply, a 429, a timeout, malformed tool arguments, an
empty reply, and **several tool calls in one turn**. Both must produce the **same normalized response objects and the same exception
types**. Otherwise the stub could behave in ways the real adapter never does, and the loop
tests would be testing a fiction.

**Real-provider fixtures.** The stub can match the adapter while both mismatch a real
provider. Groq and Gemini differ in tool-call formats, `finish_reason` values, and how they
report refusals and truncation. So the D-level tests include **at least one fixture per
real provider**, captured from an actual response (sanitized, stored with capture date and
model), not hand-written: a tool call, a text reply, and a truncation or refusal where the
provider produces one. The nightly smoke set flags when a live response no longer parses
the way the fixture says.

### Production gate
Enforced in two independent layers:
1. **Factory:** the provider factory refuses to build `ScriptedLLM` unless
   `LLM_PROVIDER=scripted` **and** `TESTING=true`.
2. **Startup check:** the service refuses to start in `APP_ENV=production` if either flag
   is set. The same startup check covers ADR-004's `CRASH_POINT` and `DEBUG_FAULT_INJECTION`
   and ADR-005's test hook.

**Gate test:** start the service as a subprocess with `APP_ENV=production` and each test
flag in turn, and assert a non-zero exit with a clear message. A gate nobody tests is a
convention.

## Consequences
- **The explicit limit of this approach is drift.** Scripts encode what we *expect* models
  to do. The check against reality is the **nightly real-model smoke set** (EVAL_PLAN)
  against Groq/Gemini. Every new failure seen in a real run (nightly, eval, or demo) becomes
  a scripted regression case, so the script library grows from observed behavior, not just
  from imagination.
- Adversarial and resolution evals (EVAL_PLAN) always use real models. The stub never
  produces a reported safety or quality number.
- The script library is test code with its own review standard: each scripted bad output
  says which real failure it represents.
- **Revisit if:** the provider interface changes significantly (the contract test fails
  first), or the nightly smoke set keeps finding behavior the scripts don't cover.

## Interview-ready summary
"My merge-gating tests run on a scripted fake model behind the same interface as the real
provider. It's strict: an unexpected call or an unused turn fails the test, so a broken
loop can't pass by accident. It records the prompts it's given, so I can assert structural
facts, like untrusted KB text sitting inside its delimited block, or no user_id in any tool
schema. Turns can be matched by content, so a legitimate change in loop order doesn't break
scripts. A contract test runs the same scenarios through the stub and the real adapter, so
the stub can't drift into behavior the real client never has. One detail I like: in the
crash tests, the resumed worker gets an empty script, which proves that resuming never
calls the model again. The limit is that scripts encode my expectations, so a nightly
real-model smoke set is the drift check, and every real failure becomes a new scripted
case. Record-and-replay would have been realistic, but it can't produce failures on
demand, and failures are the point."

# ADR-008: Run Lifecycle and Cross-ADR Clarifications

**Status:** Accepted (2026-10-08). Amends ADR-001, ADR-003, ADR-004 and ADR-005 where
listed below; where this ADR and an earlier one disagree, this ADR wins.

## Context
A consistency pass across ADR-001 to ADR-007 found three gaps:

1. **No canonical list of run statuses.** ADR-001 names `EXECUTE`, `RESPOND` and
   `ESCALATE`. ADR-004/005 use `APPROVED`, `EXECUTING`, `NEEDS_RECONCILIATION`,
   `COMPLETED` and `ESCALATE`. ADR-005 requires an enum but never defines it. `ESCALATE`
   is used both as a terminal status (ADR-003 expiry, ADR-005 rejection at execution) and
   as a flag on a run that's still being reconciled (ADR-004 step 4). Counted naively, there
   are about 10 states, past ADR-001's own "revisit above 8" threshold.
2. **Nobody owns `NEEDS_RECONCILIATION` runs.** Workers claim only `APPROVED` and
   `EXECUTING` (ADR-004 step 7), so a run that reaches reconciliation is never resolved by
   code; only the stale-unknown alert fires.
3. **Smaller mismatches:** `last_attempt_at` missing from ADR-005's transition, ADR-003's
   "enqueues execution" with no queue, no stated place where `review_deadline` is set, and
   the per-PR real-model smoke set conflicting with ADR-006's no-network gating tests.

ADR-002 to ADR-007 are locked, so the fixes are recorded here.

## Options Considered

### Part 1 — How run state is modeled

#### Option A: Seven saved statuses, transient steps, a refund-outcome field, and database invariants — CHOSEN
- How it works: Only states a run can **rest in** are saved as `status`. Steps that
  happen inside one transaction (`INTAKE`, `POLICY_CHECK`, `RESPOND`) aren't saved.
  `refund_outcome` (ADR-004) records whether money moved, separately from status.
  `escalated_at` is a flag meaning "a human has been asked to look", allowed on any
  status. `CHECK` constraints make illegal combinations impossible to write.
- Pros: Seven statuses, under ADR-001's threshold. Workflow state and business facts
  stay separate (the ADR-004 lesson). The database rejects inconsistent rows even if code
  has a bug.
- Cons: Readers have to learn the difference between statuses and steps. The constraints
  have to change when the lifecycle does.

#### Option B: Save every step as a status — rejected
- How it works: `INTAKE`, `POLICY_CHECK`, `RESPOND` and so on are all statuses.
- Pros: Every step is visible in the table.
- Cons: About 10 states. Steps that never outlive a transaction look like places a run
  can get stuck, and every one needs claim and resume rules.
- Why we didn't use it here: It adds states nothing can rest in.
- When it WOULD be the better choice: Long-running steps that really do pause between
  transactions (for example a multi-minute external check).

#### Option C: A small status plus many boolean flags — rejected
- How it works: For example `status ∈ {open, closed}` plus `is_approved`,
  `is_executing`, `needs_reconciliation`…
- Pros: Flexible; adding a flag is easy.
- Cons: Many flag combinations are meaningless, and constraints to forbid them get
  complicated. That's the same mixing of concerns ADR-004 removed.
- Why we didn't use it here: Illegal states become easy to represent.
- When it WOULD be the better choice: Orthogonal attributes that really do combine
  freely.

#### Option D: Derive state from the event log (event sourcing) — rejected
- How it works: The `events` table is the source of truth, and status is computed by
  replaying it.
- Pros: Full history and natural auditing.
- Cons: Compare-and-set claims, fencing and the capacity query (ADR-004/005) all need a
  current-state row to lock and update. Projections add complexity.
- Why we didn't use it here: The safety mechanisms are built on conditional updates of
  a current-state row.
- When it WOULD be the better choice: Audit-heavy domains where history matters more
  than locking current state.

### Part 2 — Who resolves `NEEDS_RECONCILIATION`

#### Option A: An automated reconciler that never sends again — CHOSEN
- How it works: A reconciler claims `NEEDS_RECONCILIATION` runs with the same lease and
  fencing rules (ADR-004 step 7), waits for the quiet period, and calls the ADR-004 step 5
  lookup. Found → `refund_outcome = confirmed`, status `COMPLETED`. Not found after the
  quiet period → `refund_outcome = rejected`, status `ESCALATED`. **It never sends a
  refund.** A human decides whether a new refund should be proposed, which would be a new
  action with a new key.
- Pros: Most uncertain runs resolve without a human. Never re-sending removes the
  double-refund risk an old key could create.
- Cons: Another claimable status, and another loop to test.

#### Option B: Human-only resolution — rejected
- How it works: The alert fires, and a person checks refund-api and sets the outcome.
- Pros: No code path can get it wrong.
- Cons: Every timeout becomes manual work, and capacity stays reserved until someone acts.
- Why we didn't use it here: Most reconciliations are a simple lookup.
- When it WOULD be the better choice: A provider with no lookup endpoint, or very low
  volumes.

#### Option C: Reconcile and re-send automatically if not found — rejected
- How it works: Like A, but "not found" triggers a new send.
- Pros: Fewest escalations.
- Cons: If a hung request commits after the lookup, or the key was pruned, this refunds
  twice. That's exactly what ADR-004 exists to prevent.
- Why we didn't use it here: It trades a small amount of human work for a money bug.
- When it WOULD be the better choice: Never for money movement; possibly for idempotent,
  harmless actions.

## Decision

### Saved statuses (the `run_status` enum)
| Status | Meaning | Who moves it on | Terminal |
|---|---|---|---|
| `AGENT_STEP` | Conversation in progress; the model loop may run | The API request handling the customer's message | No |
| `AWAIT_APPROVAL` | A high-risk proposal waits for a reviewer | Reviewer action or expiry | No |
| `APPROVED` | Approved (by policy or a reviewer); nothing sent yet. **This status is the execution queue** | Worker (claims it) | No |
| `EXECUTING` | Refund may have been sent; outcome `unknown` | Worker (claims it) | No |
| `NEEDS_RECONCILIATION` | Outcome still unknown after bounded retries or an unclassifiable response | Reconciler (claims it) | No |
| `COMPLETED` | Done: answered, refunded, or a reviewer declined | — | Yes |
| `ESCALATED` | Handed to a human; nothing more runs automatically | — | Yes |

`INTAKE`, `POLICY_CHECK` and `RESPOND` are **steps inside transactions**, not statuses.
Where ADR-003/004/005 write `ESCALATE`, read `ESCALATED`. ADR-001's `EXECUTE` is
`APPROVED → EXECUTING`.

### Transitions (anything not listed is illegal)
| From → To | Trigger | Same transaction also |
|---|---|---|
| (new) → `AGENT_STEP` | `INTAKE` | Customer upsert (ADR-005), past-ticket digest (ADR-002) |
| `AGENT_STEP` → `COMPLETED` | Model reply with no proposal (answer, FAQ) | Reply event |
| `AGENT_STEP` → `AWAIT_APPROVAL` | Refund proposal, `POLICY_CHECK` = high risk | Sets **`review_deadline`**, appends `pending_review` event |
| `AGENT_STEP` → `APPROVED` | Refund proposal, `POLICY_CHECK` = in policy, low risk | Creates the idempotency key and saves the canonical payload (ADR-004 step 1) |
| `AGENT_STEP` → `ESCALATED` | `POLICY_CHECK` out of policy, an escalate proposal, step budget or malformed-retry budget used up | `escalated_at`, `escalated` event |
| `AWAIT_APPROVAL` → `APPROVED` | Reviewer approves (compare-and-set, deadline not passed; ADR-003) | Creates the key, saves the payload, `approved` event |
| `AWAIT_APPROVAL` → `COMPLETED` | Reviewer declines (compare-and-set) | Template reply event |
| `AWAIT_APPROVAL` → `ESCALATED` | Expiry (compare-and-set, ADR-003) | `escalated_at`, `escalated` event |
| `APPROVED` → `EXECUTING` | Execution-time policy check passes (ADR-005 locked transaction) | `first_send_started_at`, **`last_attempt_at`** (same value), `refund_outcome = unknown` |
| `APPROVED` → `ESCALATED` | Execution-time policy check rejects | `escalated_at`, `escalated` event, worker claim cleared |
| `EXECUTING` → `COMPLETED` | 2xx (fresh or replayed) | `refund_outcome = confirmed`, template reply event |
| `EXECUTING` → `ESCALATED` | 400 with a recognized business-error code | `refund_outcome = rejected`, `escalated_at`, template reply event |
| `EXECUTING` → `NEEDS_RECONCILIATION` | Retries exhausted, 422, 401/403/404, or any unrecognized response | `escalated_at`, alert, outcome stays `unknown` |
| `NEEDS_RECONCILIATION` → `COMPLETED` | Reconciler finds the refund | `refund_outcome = confirmed`, template reply event |
| `NEEDS_RECONCILIATION` → `ESCALATED` | Reconciler finds none **after the quiet period** | `refund_outcome = rejected`, template reply event |

Every move into `COMPLETED` or `ESCALATED` clears `worker_id` and `lease_expires_at`.

### Database invariants (`CHECK` constraints)
- `refund_outcome = 'none'` if and only if `first_send_started_at IS NULL`.
- `status IN ('AGENT_STEP', 'AWAIT_APPROVAL', 'APPROVED')` ⇒ `refund_outcome = 'none'`.
- `status IN ('EXECUTING', 'NEEDS_RECONCILIATION')` ⇒ `refund_outcome = 'unknown'`.
- `status IN ('COMPLETED', 'ESCALATED')` ⇒ `refund_outcome <> 'unknown'` **and**
  `worker_id IS NULL`. **A run can't finish while money movement is uncertain**; an
  uncertain run is always owned by the reconciler.
- `status = 'AWAIT_APPROVAL'` ⇒ `review_deadline IS NOT NULL`.

### Claiming (amends ADR-004 step 7)
- Workers claim `APPROVED` and `EXECUTING`. The reconciler claims
  `NEEDS_RECONCILIATION`. Both use the same compare-and-set, lease, heartbeat and fencing
  rules; only the status list differs.
- `AGENT_STEP` isn't claimed. It's driven by the request handling the customer's message.
  If that process dies mid-turn, the turn is lost, but nothing was sent (only read tools
  run there), and the customer's next message continues the run. Only one turn per
  conversation runs at a time (code-level guard).

### Wording clarifications
- **ADR-003 "enqueues execution":** there's no separate queue. Being `APPROVED` *is* the
  queue that workers claim from.
- **ADR-005 step 4:** the move to `EXECUTING` also sets `last_attempt_at`, as ADR-004
  step 6 says. ADR-004 step 6 is authoritative.
- **ADR-004 step 3 table:** "Stop; escalate; alert" for 422 means `NEEDS_RECONCILIATION`
  with `escalated_at` set (the outcome is still unknown). "Escalate" for a 400 business
  error means `ESCALATED` with `refund_outcome = rejected`.
- **`review_deadline`** is set in the same transaction that moves a run into
  `AWAIT_APPROVAL`.
- **The per-PR real-model smoke set is informational, not a merge gate.** Merge gates are
  the deterministic suites only (ADR-006). The smoke set reports on the PR but never
  blocks it, and it's skipped when no API key is available.

## Consequences
- The run lifecycle has one source of truth (this ADR), and the database enforces its
  invariants.
- The reconciler is a third claimant with its own tests (EVAL_PLAN).
- **Revisit if:** a new resting state is needed (an eighth status means re-checking
  ADR-001's threshold), or a real provider without a lookup endpoint is integrated (then
  Part 2 Option B).

## Interview-ready summary
"I separated where a run is from whether money moved. There are seven saved statuses.
Steps like the policy check happen inside a transaction and aren't statuses, so nothing
can get stuck in them. A separate refund-outcome field records whether money moved. The
database enforces the important rules with CHECK constraints. The big one: a run can't
reach a final status while its refund outcome is unknown, so uncertainty always has an
owner. That owner is a reconciler that claims uncertain runs with the same lease and
fencing as the workers, waits out a quiet period, and looks the refund up. Crucially, it
never re-sends. If the refund isn't there, a human decides, because an automatic re-send
after a 'not found' is exactly how you refund someone twice. I considered event sourcing,
but my safety mechanisms need a current-state row to lock and conditionally update."

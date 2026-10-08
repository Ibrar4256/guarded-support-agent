# ADR-009: Refund Policy Rules and Decision Audit

**Status:** Accepted (2026-10-08). Revised the same day across four review passes:
rev 1 (rule IDs, decision log, fail closed, approval binding); rev 2 (kill switch at
pre-send, single currency, review caps, self-approval, live actions, one unfinished run per
conversation); rev 3 (pending exposure, hold-don't-escalate, `review_hash`, R18/R19,
C01 on customer messages only, input snapshot); rev 4 (two-phase R16/R19, attention
flags, fulfillment-only order status, owner vs. runtime roles, `review_revision`, kill
switch vs. breaker scope, fenced failure transactions, `AGENT_STEP` resumption).

**Locked 2026-10-08.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Precedence:** later amending ADRs win: **ADR-009 > ADR-008 > ADR-001–007**.

**Amends:**
- **ADR-001:** the safety claim is restated precisely (see *Safety claim*).
- **ADR-003:** approvals require the matching `review_hash` and `review_revision` and
  reject self-approval; a third role, `admin`, controls the breaker and kill switch.
- **ADR-004/005:** their capacity sums add `amount_minor` without a currency filter, which
  is only correct for one currency. **v1 enforces a single configured currency with a rule
  (R15) and a startup check**, not a `CHECK` constraint (config can change). The
  per-customer lock also covers the **whole proposal-time evaluation**, not only the
  pre-send check. **Each database gets two roles instead of one:** an owner/migration role
  and a runtime role that doesn't own any table (needed for the append-only log, below).
- **ADR-008:** new triggers on existing transitions (`C01`, `FAIL_CLOSED`, R16/R17/R19),
  a worker-claim filter while sends are held, approval fields and `CHECK`s, `action_id`
  assigned at proposal time, escalation resolution, and **at most one unfinished run per
  conversation**. **No new transitions:** the only way a run starts is still
  `(new) → AGENT_STEP`; reviewer-originated refunds are out of scope for v1.

## Context
ADR-001–008 fix *how* policy is enforced but not *which rules* it contains. Step 1
implemented seven rules as a first proposal. Reviews of the policy found these problems;
the table at the end of *Decision* maps each one to its resolution.

1. **Split refunds:** the auto-approve maximum looks at one refund at a time.
2. **Asking twice:** idempotency keys stop retries of *one* action, not a second action.
3. **Missing eligibility rules:** refund window, order status, open chargeback.
4. **No brake on a mass campaign or a policy bug**, including refunds already queued.
5. **Multiple currencies** make single thresholds meaningless, and ADR-004/005 sums ignore
   currency.
6. **Review-queue flooding.**
7. **Legal/abusive-language escalation** (project scope) isn't implemented.
8. **No audit trail, no binding of approvals to what the reviewer saw, no reviewer
   identity, self-approval possible.**
9. **Failure behavior** of the policy itself is undefined.
10. **Customer messages after a proposal** are undefined.
11. **Re-asking after an escalation or a decline** would start a fresh run that could
    auto-approve.

## Options Considered

### Option A: Rules as pure code with stable rule IDs, plus an append-only decision log — CHOSEN
- How it works: Each rule is a small pure function in `agent.core.policy` with a stable
  ID. The policy evaluates **every** rule, records every rule that fired, and the most
  severe outcome wins. Each policy decision and human action is written to an append-only
  `decisions` table.
- Pros: Deterministic, unit- and property-testable with no database; every decision is
  explainable and countable; an input snapshot makes replay against a new policy version
  possible later.
- Cons: Changing a rule needs a deploy.
- Cost/latency/complexity profile: Microseconds per decision; one insert per decision.

### Option B: A policy engine or rules DSL (OPA/Rego, json-logic) — rejected
- How it works: Rules in a policy language, evaluated by an engine.
- Pros: Rules change without a deploy.
- Cons: Another runtime and language; rules leave the type-checked, import-restricted core.
- Why we didn't use it here: Few rules and one developer; core purity is worth more.
- When it WOULD be the better choice: Many policies maintained by non-engineers, or one
  policy shared across services in different languages.

### Option C: Let the model judge whether a refund is reasonable — rejected
- How it works: An LLM decides or scores each proposal.
- Pros: Nuance.
- Cons: The model would enforce policy (ADR-001 forbids it); injection could talk it into
  approving.
- Why we didn't use it here: It breaks the core safety principle.
- When it WOULD be the better choice: Never for hard limits. A model signal may only
  **raise** severity (see *Asymmetry principle*).

### Option D: Keep the step-1 policy — rejected
- How it works: Seven rules, free-text reasons, no log.
- Pros: Already written.
- Cons: Leaves problems 1, 2, 4, 6, 8 and 11 open.
- Why we didn't use it here: Known abuse paths and no audit trail.
- When it WOULD be the better choice: A throwaway demo with no money.

## Decision

### Outcomes and severity
| Outcome | Meaning | ADR-008 transition (at proposal) |
|---|---|---|
| `FAIL_CLOSED` | Evaluation couldn't complete | → `ESCALATED`, nothing sent |
| `OUT_OF_POLICY` | A **hard rule** failed | → `ESCALATED`, nothing sent; never enters review |
| `REVIEW` | A human must decide | → `AWAIT_APPROVAL` |
| `AUTO` | Every rule passed | → `APPROVED`, `approval_source = policy` |

**Severity order: `FAIL_CLOSED > OUT_OF_POLICY > REVIEW > AUTO`.** A **hard rule** is any
rule whose outcome is `OUT_OF_POLICY`. Adding a fired rule can never lower the result.

**Asymmetry principle:** content and model signals (C01, and any future classifier) may
only **raise** severity, never lower it. Money decisions are lowered only by deterministic
rules over database facts.

### Definitions
- **Capacity** (unchanged, ADR-004): `unknown` + `confirmed` refunds. Used by R05/R06 at
  pre-send, where it's authoritative.
- **Pending exposure** (new, a **different metric**; ADR-004's capacity is untouched):
  the amounts of other actions that are `AWAIT_APPROVAL`, or `APPROVED` with outcome
  `none`. Used **only at proposal time**, by R05, R06, R11 and R12, so a second proposal
  sees the first even before anything is sent.
- **Policy clock:** the database clock (`now()`, read once per transaction, UTC) passed
  into `core` (ADR-007).
- **Window boundaries are inclusive:** `now − delivered_at ≤ refund_window`.
- **`action_id`** is assigned when a refund proposal is validated (in `AGENT_STEP`). The
  idempotency key and canonical payload are still created at approval (ADR-008).
- **Live policy-approved action** (R11 and R13): `approval_source = policy` and either
  status `APPROVED` with outcome `none`, or outcome `unknown`/`confirmed`. Its **window
  anchor is `approved_at`** (an `APPROVED` run has no `first_send_started_at` yet). R11 sums
  them per customer over `per_customer_window`; R13 sums them system-wide over
  `breaker_window`.
- **Order lookup log** (R02): every order the run's order-lookup tool returns is
  **persisted on the run** (`run_order_lookups`), because the pre-send re-check reads it
  after the turn has ended. A lookup from an earlier run doesn't count, so every run must
  look the order up again; prompts and scripts account for that.
- **`orders.status` is fulfillment status only:** `pending`, `shipped`, `delivered`,
  `cancelled`. Refund state is **never** written to the order; it's always derived from
  refund history (R05, R12). Otherwise a partial refund would make every later refund
  fail R09.
- **Attention flags:** `escalated_at` means "a human was alerted" (ADR-004/008, including
  reconciliation alerts). `c01_flagged_at` means "the customer made a legal threat or was
  abusive". They're separate so a reconciliation alert never affects R18.

### Rules
| ID | Rule (passes when…) | Inputs | Outcome if it fails | Proposal | Pre-send | Config |
|---|---|---|---|---|---|---|
| `R01_AMOUNT_POSITIVE` | amount > 0 | proposal | OUT_OF_POLICY | ✓ | ✓ | — |
| `R02_ORDER_PROVENANCE` | the order was returned by this run's order-lookup tool, and the record loaded for the proposal has the same ID | proposal, run's lookup log, order | OUT_OF_POLICY | ✓ | ✓ | — |
| `R03_ORDER_OWNERSHIP` | the order belongs to the session user | order, session | OUT_OF_POLICY | ✓ | ✓ | — |
| `R04_CURRENCY_MATCH` | proposal currency = order currency | proposal, order | OUT_OF_POLICY | ✓ | ✓ | — |
| `R05_ORDER_CHARGED_CAP` | order's capacity (+ pending exposure at proposal) + amount ≤ charged | order, refund history | OUT_OF_POLICY | ✓ | ✓ | — |
| `R06_CUSTOMER_LIMIT` | customer's `unknown` (any age) + `confirmed` (in window) (+ pending exposure at proposal) + amount ≤ limit | refund history | OUT_OF_POLICY | ✓ | ✓ | `per_customer_limit_minor`, `per_customer_window` |
| `R07_AUTO_APPROVE_MAX` | amount ≤ maximum | proposal | REVIEW | ✓ | — | `auto_approve_max_minor` |
| `R08_REFUND_WINDOW` | order delivered and `now − delivered_at ≤ window` | order, clock | OUT_OF_POLICY | ✓ | — | `refund_window` |
| `R09_ORDER_STATUS` | fulfillment status = `delivered` (not `pending`, `shipped` or `cancelled`) | order | OUT_OF_POLICY | ✓ | ✓ | — |
| `R10_NO_CHARGEBACK` | no open chargeback | order | OUT_OF_POLICY | ✓ | ✓ | — |
| `R11_AUTO_APPROVE_BUDGET` | customer's live policy-approved actions with `approved_at` in the window + amount ≤ budget | refund history | REVIEW | ✓ | — | `auto_approve_budget_minor`, `per_customer_window` |
| `R12_OTHER_ACTION_ON_ORDER` | no other action on this order in pending exposure or with outcome `unknown`/`confirmed` | refund history | REVIEW | ✓ | — | — |
| `R13_CIRCUIT_BREAKER` | breaker not tripped, and system-wide policy-approved amount in the breaker window + amount ≤ threshold | controls, global totals | REVIEW (and returns a **trip signal**) | ✓ | — | `breaker_window`, `breaker_threshold_minor` |
| `R14_SENDS_HELD` | not held: the breaker holds `approval_source = policy` runs; the kill switch holds **all** runs | controls | **HOLD** (not a decision outcome; see *Breaker and kill switch*) | — | ✓ | — |
| `R15_SUPPORTED_CURRENCY` | currency = configured v1 currency | proposal | OUT_OF_POLICY | ✓ | ✓ | `currency` |
| `R16_PENDING_REVIEW_CAP` | **only if the result would be REVIEW:** customer's `AWAIT_APPROVAL` runs < cap | run counts | OUT_OF_POLICY | ✓ | — | `pending_review_cap` |
| `R17_DAILY_PROPOSAL_CAP` | customer's proposals in the last 24 h < cap | run counts | OUT_OF_POLICY | ✓ | — | `daily_proposal_cap` |
| `R18_STICKY_ESCALATION` | customer has no run that is (status `ESCALATED` **or** `c01_flagged_at` set) **and** `escalation_resolved_at` null; and no reviewer decline on this order within the cooldown | run history | REVIEW | ✓ | — | `decline_cooldown` |
| `R19_GLOBAL_REVIEW_QUEUE_CAP` | **only if the result would be REVIEW:** system-wide `AWAIT_APPROVAL` count < cap | global counts | OUT_OF_POLICY | ✓ | — | `global_review_queue_cap` |

**Two-phase evaluation.** Phase 1 evaluates every rule except R16 and R19 and takes the
most severe outcome. **Phase 2 applies R16 and R19 only if that outcome is `REVIEW`**:
they're queue-admission checks, so a refund that would auto-approve is never blocked by a
full review queue.

**The cascade is intended:** a tripped breaker makes proposals `REVIEW` (R13); if the global
queue is full they escalate (R19); and R18 then keeps those customers in review until a
human resolves the escalation. Without that stickiness, a calm re-ask could auto-approve
while a human is also following up on the escalation, and the customer could be paid twice.

**Pre-send re-checks** (ADR-004 step 6, under the customer lock): R01–R06, R09, R10, R14,
R15, using **ADR-004 capacity** (not pending exposure) and the **current policy
version**. Review rules aren't re-run (an approved action isn't sent back to review).
R08 isn't re-checked: the customer asked inside the window, and a slow reviewer mustn't
push them out of it. A chargeback opened while the refund waits in `APPROVED` is caught by
R10; **one opened after the refund is `EXECUTING` can't be stopped** (it may already be
sent), which is documented.

### Approval rules (in the ADR-003 approval compare-and-set)
- `A01_REVIEW_HASH`: `review_hash` = SHA-256 of canonical JSON of `order_id`,
  `amount_minor`, `currency`, `action_id`, the sorted fired rule IDs and `policy_version`,
  computed **at proposal time** and stored. The approve request carries the hash the
  reviewer saw, and the update requires `AND review_hash = :h`. A mismatch returns **409**.
  This is **agent-only** (test vectors in the agent's tests, not `contracts/`); it is *not*
  the ADR-004 payload hash. Since `review_hash` never changes after the proposal, A01 in
  practice catches client bugs and tampered requests; **A03 is what catches a stale
  review**. Both exist for those two different failures.
- `A03_REVIEW_REVISION`: `runs.review_revision` starts at 0 and is incremented by anything
  that changes what the reviewer should see: a C01 flag after the proposal, and **any
  customer message that arrives during `AWAIT_APPROVAL`** (it's shown to the reviewer). The
  approve request carries the revision the reviewer loaded, and the update requires `AND
  review_revision = :rev`. A reviewer whose page predates a legal threat gets **409** and
  must reload. (`review_hash` doesn't change in that case, so A01 alone can't catch it.)
- `A02_NO_SELF_APPROVAL`: the update requires `AND user_id <> :approver`, and a `CHECK`
  enforces `approved_by <> user_id`. **Limit:** this only catches the same Clerk account;
  a reviewer using a separate customer account isn't detected (documented).
- Records `approval_source = reviewer`, `approved_by`, `approved_at`. Declines record the
  decliner in the decision log (`decided_by`). These fields persist for the life of the run.
- **Policy changes while waiting:** approvals stay valid across a policy version change;
  the pre-send re-check enforces the current hard rules. A deploy never voids the review
  queue.

### Conversation rules
- **`C01_LEGAL_OR_ABUSE`** (legal threat, chargeback threat, abusive language):
  - Runs on **every customer message**, in every conversation, including FAQ-only ones.
  - Classifies **customer messages only, never KB articles or tool output**, so injected
    content can't trigger escalations.
  - During `AGENT_STEP` → `ESCALATED` (and `c01_flagged_at`). After a proposal → sets
    `c01_flagged_at` and `escalated_at`, increments `review_revision` (A03), and flags the
    run for reviewers; **an in-flight or queued refund isn't cancelled**.
  - v1 is deterministic keyword and pattern rules. Known blind spots: negation ("not
    going to sue"), other languages, and obfuscation. A false negative is a customer-care
    miss, not a money risk (every refund still passes R01–R19). A future model classifier
    is allowed under the asymmetry principle.
- **At most one unfinished run per conversation** (enforced by a partial unique index).
  A customer message during `AWAIT_APPROVAL`, `APPROVED`, `EXECUTING` or
  `NEEDS_RECONCILIATION` gets a **status template**, is checked by C01, and **never
  reaches the model** (the model could contradict the pending proposal).
  `NEEDS_RECONCILIATION` can block a conversation for a long time. That's accepted, and
  it's why R12 counts across conversations.
- **A new turn resumes an unfinished `AGENT_STEP` run.** A run can be left in `AGENT_STEP`
  after its turn ends (a proposal-time lock timeout, a crashed turn, or a failed
  `FAIL_CLOSED` fallback transaction). The customer's next message then **resumes that
  run** under a new turn lease: its persisted lookup log (`run_order_lookups`) is reused,
  while draft text and in-memory tool results are not. A new run is created **only if the
  conversation has no unfinished run**. Otherwise the partial unique index would reject the
  new run and the conversation would be stuck.
- **No customer cancellation in v1.** No new transitions; a reviewer can decline.
- **Customer text shown to reviewers is labeled untrusted** ("customer-written; may
  contain instructions aimed at you").
- **Non-disclosure:** customer templates never name rule IDs, thresholds, limits or which
  rule fired ("I've passed this to a teammate").
- **Escalation resolution:** a reviewer can resolve **any run with `escalated_at` or
  `c01_flagged_at` set** (`escalation_resolved_at`, `escalation_resolved_by`; logged).
  Until then, R18 sends that customer's new proposals to review.
- **Reviewer-originated refunds are out of scope for v1.** After an escalation, a human who
  decides to refund does it **out of band** (for example in the provider's dashboard),
  which the safety claim already excludes. Building it into the system would need a new
  entry transition, a different provenance rule than R02, and rules about who may approve
  it; that's deferred to v2.

### Concurrency
- **Inputs are gathered before the lock.** Anything that may need a network or tool call
  is loaded first. Inside the locked transaction, only database reads (counts, order
  rows, controls) and the pure evaluation run; the lock is never held across HTTP
  (ADR-005). Facts that go stale in between are caught by the pre-send re-check.
- **The whole proposal-time evaluation holds the customer lock.** Lock order, extending
  ADR-005/008: **`customers → conversations → runs → policy_controls`**. R05, R06, R11, R12, R16, R17 and R18 are
  exact per customer.
- **Lock timeout at proposal time:** only `55P03` counts as a lock wait. It's retried a
  bounded number of times inside the turn, then the customer gets a templated "please
  try again in a moment" reply and the run **stays in `AGENT_STEP`**. Not `FAIL_CLOSED`:
  waiting on a lock isn't a policy failure (same reasoning as ADR-005).
- **R13 and R19 are approximate.** They count across customers under a per-customer lock,
  so concurrent proposals from different customers can both pass (cross-customer write
  skew). Overshoot is bounded by concurrent proposals × the auto-approve maximum. That's
  accepted for a campaign-level safety net (locking a global row would serialize every
  proposal).

### Circuit breaker and kill switch
- State lives in one `policy_controls` row (no deploy needed): `breaker_tripped_at`,
  `breaker_reset_by`, `breaker_reset_at`, and `sends_enabled` (the kill switch).
- **Two controls with different scope:**
  - The **breaker** (automatic, R13) targets policy bugs and campaigns: it holds
    **policy-approved** runs only.
  - The **kill switch** (manual, admin) targets incidents such as refund-api misbehaving,
    a provider outage or a compromised reviewer account: it holds **every** `APPROVED`
    run, reviewer-approved included.
- **Tripping:** R13 is pure, so it only returns a **trip signal**. The `db/` layer then
  runs `UPDATE policy_controls SET breaker_tripped_at = now() WHERE breaker_tripped_at IS
  NULL` in the same transaction (last in the lock order), and raises an alert if a row was
  updated. Concurrent trips are harmless.
- **While held (R14): hold, don't escalate.** Workers **don't claim** the held `APPROVED`
  runs (the claim predicate adds the condition for each control). If a hold starts
  after a claim but before the send, the pre-send re-check releases the claim and leaves
  the run `APPROVED`, nothing sent. Held runs wait; on reset they proceed through the normal
  pre-send re-check. An admin can also escalate held runs in bulk if the pause was a real
  incident. Escalating automatically would dump every legitimate refund on reviewers after
  a false alarm.
- **It never touches `EXECUTING` or `NEEDS_RECONCILIATION`** (ADR-004 step 6; they may
  already be sent).
- **Only the `admin` role** can reset the breaker or toggle the kill switch (logged).
  There's no automatic reset, so it can't flap.

### Fail closed
- **Triggers:** a rule raises; a required input is missing (order not found, no
  `delivered_at` on a delivered order); an unknown currency; an unrecognized enum value.
- **Each rule is evaluated in isolation**, so one rule raising doesn't hide the others:
  the raising rule is recorded as `FAIL_CLOSED:<rule_id>`, and every other fired rule ID
  is still logged.
- **At proposal time:** the failing transaction rolls back. A **separate transaction**,
  fenced by the turn lease (`active_turn_id = :t`, ADR-008), moves `AGENT_STEP →
  ESCALATED` and writes the `FAIL_CLOSED` log row. Nothing sent.
- **At pre-send:** nothing sent. `55P03` is retried (ADR-005). For any other error, the
  failing transaction rolls back, and a **separate transaction fenced by `claim_version`**
  increments `presend_attempts` (or, at the maximum, moves `APPROVED → ESCALATED` with the
  `FAIL_CLOSED` row).
- **Invalid configuration refuses startup** (for example a budget lower than the
  auto-approve maximum, or an unsupported currency) instead of failing at runtime.
- **If that separate transaction also fails**, an alert is raised; the run stays where it
  was (`AGENT_STEP` or `APPROVED`) with nothing sent, and the turn lease or worker lease
  expires normally.

### Decision log
- **Scope:** policy evaluations (proposal, pre-send), human actions (reviewer approve or
  decline, escalation resolution, review expiry), C01 escalations, and control changes
  (breaker trip or reset, kill switch). **Worker and reconciler HTTP outcomes are not
  decisions**; they stay in `events` (ADR-003/008).
- **Row:** `run_id`, `action_id`, `stage`, `outcome`, `rule_ids[]`, `policy_version`,
  `config_snapshot`, **`input_snapshot`** (the order facts and computed totals the rules
  saw, so decisions can be replayed later), `review_hash`, `trace_id`, `decided_by`
  (`policy` or a user ID), `decided_at`.
- **No message text in the log:** C01 records the matched pattern IDs only (PII).
  Retention period is a config value.
- **Same transaction as the fenced update**, so a 0-row fenced update also rolls back its
  log row (except `FAIL_CLOSED`, above).
- **Append-only by permissions:** `REVOKE UPDATE, DELETE, TRUNCATE ON decisions FROM
  <runtime role>`; the runtime role has `INSERT` and `SELECT` only. **This only works if the
  runtime role doesn't own the table**, because an owner can grant privileges back to
  itself. So each database has an **owner/migration role** (runs Alembic, owns tables) and
  a separate **runtime role** (the services connect as it).
- **Retention:** a scheduled job running as the owner role deletes rows older than
  `decision_retention`. The runtime role can never delete.

### Safety claim (amends ADR-001)
**No code path in this system sends a refund for a proposal that failed a hard rule when it
was proposed, and the payment-blocking rules (R01–R06, R09, R10, R14, R15) are checked
again immediately before every first send. No reviewer can approve past a hard rule,
because out-of-policy proposals never enter review.** This assumes the rule code is correct
(R13/R14 and the property tests exist for when it isn't) and covers only refunds issued
through this system.

### Resolution of each context item
| # | Problem | Resolved by |
|---|---|---|
| 1 | Split refunds | R11 (budget incl. pending), R12, pending exposure, customer lock |
| 2 | Asking twice | R12 across conversations, one unfinished run per conversation |
| 3 | Eligibility | R08, R09, R10 |
| 4 | Campaign or policy bug | R13 breaker, R14 hold (queued runs), kill switch, admin-only reset |
| 5 | Currencies | R15 single currency, startup check, amended wording |
| 6 | Queue flooding | R16, R17, R19 |
| 7 | Legal/abuse | C01 (customer messages only, asymmetry principle) |
| 8 | Audit, approvals | decision log, A01 `review_hash`, A02, A03 `review_revision`, reviewer identity |
| 9 | Failure behavior | FAIL_CLOSED triggers, per-rule isolation, separate log write, startup config validation |
| 10 | Messages after a proposal | status templates, no model, no cancellation in v1 |
| 11 | Re-asking after escalation/decline | R18, escalation resolution |

### ADR-008 effects (no new transitions)
| Transition | New trigger or effect |
|---|---|
| `AGENT_STEP → ESCALATED` | C01; OUT_OF_POLICY from R15–R17, R19; FAIL_CLOSED |
| `AGENT_STEP → AWAIT_APPROVAL` | REVIEW from R13, R18 (plus R07, R11, R12) |
| `AGENT_STEP → APPROVED` | Records `approval_source = policy`, `approved_at`, `review_hash` |
| `AWAIT_APPROVAL → APPROVED` | A01 + A02 + A03 in the compare-and-set; records reviewer identity |
| `APPROVED → EXECUTING` | Worker claim excludes held runs (R14: breaker → policy-approved; kill switch → all) |
| `APPROVED → ESCALATED` | FAIL_CLOSED after `presend_attempts` max |
| (no transition) | C01 after a proposal sets `c01_flagged_at`/`escalated_at` and bumps `review_revision`; escalation resolution on any flagged run |

### Schema additions (settled before migrations, step 2)
- `orders`: `status`, `delivered_at`, `chargeback_open`.
- `run_order_lookups` (`run_id`, `order_id`, `looked_up_at`): the persisted R02 lookup
  log.
- `runs`: `action_id` (assigned at proposal, **`UNIQUE`**), `review_hash`,
  `review_revision` (default 0), `c01_flagged_at`,
  `approval_source` (`policy` | `reviewer`), `approved_by`, `approved_at`,
  `presend_attempts` (default 0), `escalation_resolved_at`, `escalation_resolved_by`,
  **`UNIQUE (idempotency_key)`**; indexes for R11/R12/R13/R16/R17/R19; a partial unique
  index on `conversation_id WHERE status NOT IN ('COMPLETED', 'ESCALATED')`.
- `CHECK`s: `approval_source = 'reviewer'` ⇒ `approved_by IS NOT NULL`;
  `approval_source = 'policy'` ⇒ `approved_by IS NULL`; `approved_by <> user_id`;
  statuses `APPROVED`/`EXECUTING`/`NEEDS_RECONCILIATION` ⇒ `approved_at IS NOT NULL`;
  **`refund_outcome <> 'none'` ⇒ `approved_at IS NOT NULL AND approval_source IS NOT
  NULL`** (covers `COMPLETED` refunds, which the status-based check misses).
- `decisions` (append-only by grants) and `policy_controls` (one row).
- Clerk roles: `customer`, `reviewer`, **`admin`**.
- Database roles: an owner/migration role and a runtime role per database (amends
  ADR-005).

### Config (all from settings, validated at startup; examples only)
`currency` (USD), `auto_approve_max_minor` (5 000), `auto_approve_budget_minor` (10 000),
`per_customer_limit_minor` (20 000), `per_customer_window` (30 days), `refund_window`
(30 days), `breaker_window` (1 hour), `breaker_threshold_minor`, `pending_review_cap` (3),
`daily_proposal_cap`, `global_review_queue_cap`, `decline_cooldown`,
`presend_max_attempts`, `decision_retention`.

### Tests (each with a negative control that must fail)
| Test | Negative control |
|---|---|
| Unit test per rule ID, firing and not firing | Rule disabled → its firing case passes wrongly |
| Property (Hypothesis): `AUTO` never has amount ≤ 0, never exceeds charged, never cross-user or other currency, never exceeds the budget across any split; evaluation never raises; **adding a fired rule never lowers severity** | A planted bug (rule returns pass) is found by Hypothesis |
| Split refund across **two conversations** → exactly one auto-approves | Pending exposure removed → both auto-approve |
| Small refund while a large one is in review (other conversation) → REVIEW | Pending exposure without `AWAIT_APPROVAL` → auto-approves |
| R11/R12 concurrent proposals (controlled timing) → exactly one auto-approves | No lock → both do |
| Re-asking after an unresolved escalation, or after a decline within the cooldown → REVIEW | R18 disabled → auto-approves |
| Stale `review_hash` → 409 | Hash check removed → approval succeeds |
| Self-approval rejected by API and `CHECK` | Each check removed alone → the other still rejects |
| Tripped breaker: policy-approved `APPROVED` runs unclaimed, `EXECUTING` untouched, reviewer-approved runs proceed | Claim filter removed → held runs get claimed |
| A rule raises → `FAIL_CLOSED`, other fired rule IDs still logged | No isolation → other IDs missing |
| Log write fails during FAIL_CLOSED → nothing sent, alert | — (asserts zero sends) |
| `UPDATE`/`DELETE` on `decisions` as the app role → denied | Superuser can (proves the grant is what blocks it) |
| No customer template contains a rule ID, threshold or limit | A planted template with a number is caught |
| C01 on a KB article containing "lawsuit" → **no** escalation; on a customer message → escalation | — |
| Message after a proposal → status template, model never called (ScriptedLLM empty script) | — (strict mode fails on any call) |
| Escalated-before-send runs stop counting toward R11, R12, R16 | — |
| Invalid config refuses startup | Valid config starts |
| Lock timeout at proposal → next message proposes successfully on the **same `run_id`** | Next message tries a new run → unique violation |
| Customer message during `AWAIT_APPROVAL` → stale `review_revision` approval gets 409 | Only C01 bumps the revision → approval succeeds |
| Role isolation (ADR-005, now **four** roles): each runtime role can't connect to the other database, and neither runtime role owns any table | Runtime role made owner → test fails |
| Full review queue (R16/R19) + a refund that would auto-approve → **AUTO** | Single-phase evaluation → wrongly escalated |
| Full queue + a refund that would need review → `ESCALATED` | R16/R19 removed → `AWAIT_APPROVAL` |
| Reconciler-confirmed run with `escalated_at` → R18 **doesn't** fire | R18 reading `escalated_at` → fires wrongly |
| C01 after the proposal → stale `review_revision` approval gets 409 | Revision check removed → approval succeeds |
| Kill switch holds reviewer-approved runs too; breaker holds only policy-approved | Breaker scope used for the kill switch → reviewer-approved run sent |
| Partial refund confirmed → a second refund on the order is REVIEW (R12), not OUT_OF_POLICY (R09) | Refund state written into `orders.status` → R09 fires |
| Runtime role doesn't own `decisions` and can't `DELETE`; the retention job (owner role) can | Runtime role as owner → can re-grant itself `DELETE` |
| FAIL_CLOSED at proposal → run `ESCALATED` via the separate transaction fenced by the turn lease | Stale turn lease → 0 rows, run unchanged |

### Deferred (out of v1)
Multiple currencies and FX, customer cancellation, reviewer-originated refunds, store credit, item-level refunds and
excluded categories, refund-rate and new-account signals (cheap later from the decision
log), per-reason rules (`reason` stored for audit only), shadow mode, alerting on
decision-mix shifts (Project 5), four-eyes approval, a model classifier for C01, and a
decision replay tool (the input snapshot makes it possible).

## Consequences
- Every decision, human or automated, is explained by rule ID, version, config and input
  snapshot; the application can't edit the log.
- Known abuse paths (split refunds, asking twice across conversations, queue flooding,
  threshold probing, self-approval, re-asking after escalation) are closed or sent to a
  person.
- A breaker or kill switch holds queued policy-approved refunds without burying reviewers.
- More work goes to reviewers. That's intended for v1; the log's rule counts show where to
  tune.
- v1 is single-currency, has no customer cancellation, and R13/R19 are approximate.
- A chargeback opened after sending can't be stopped by this system.
- **The kill switch doesn't stop `EXECUTING` retries.** If refund-api itself misbehaves,
  in-flight runs keep retrying with their key until ADR-004's retry bound moves them to
  `NEEDS_RECONCILIATION`. That's correct under ADR-004 step 6 (they may already be sent),
  and it's a known behavior.
- ADR-005's role-isolation test now covers four database roles (owner and runtime for each
  database).
- **"Item never arrived"**, one of the most common real refund reasons, is always
  `OUT_OF_POLICY` in v1: R09 requires `delivered`, and R08 needs `delivered_at`. Those
  customers reach a human. This is a deliberate v1 choice, not an oversight.
- Reviewer-originated refunds and customer cancellation are out of band or absent in v1.
- Every run must look the order up again (R02), which costs one extra tool call per
  refund conversation.
- **Revisit if:** non-engineers need to edit rules (Option B), reviewer load becomes the
  bottleneck, multiple currencies are needed, or a provider's fraud signals can replace
  some rules.

## Interview-ready summary
"The model only proposes a refund; a pure, deterministic policy in code decides. Each rule
has a stable ID, every rule is evaluated, the most severe outcome wins, and the decision
is written to an append-only log with the rule IDs, policy version, config and an input
snapshot, so any decision can be explained or replayed. The interesting part wasn't the
thresholds, it was the abuse paths between them. A per-refund auto-approve limit is
beaten by splitting, so there's a budget that counts approved-but-not-yet-sent refunds as
'pending exposure', evaluated under a per-customer lock so two simultaneous proposals
can't both slip through. Asking twice, flooding the review queue and re-asking after an
escalation each have their own rule. Out-of-policy proposals never reach a reviewer, so
no human can approve past a hard limit, and approvals are bound to a hash of exactly what
the reviewer saw plus a revision counter. A circuit breaker holds auto-approved refunds
instead of escalating them, so a false alarm doesn't bury reviewers, and a separate kill
switch holds everything during an incident. Anything that throws fails closed. I chose
code over a policy engine like OPA because, with a handful of rules and one developer,
keeping the policy pure, typed and property-tested mattered more than editing rules
without a deploy; OPA would win once non-engineers own the rules."

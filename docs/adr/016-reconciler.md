# ADR-016: The Reconciler — Where It Runs, What Its Quiet Period Covers, and Failed Lookups

**Status:** Accepted (2026-11-12). Revised the same day after two outside reviews. First pass:
the quiet period also covers a budget raised between the config read and the send (a fresh read
at reconcile time); a refund-api outage closes the gate instead of counting toward every run's
streak; the kill switch stops the reconciler from acting on "not found"; strong consistency
stated as an assumption; per-run page limiting and the version bump made explicit. Second pass:
every reconciler release applies the cooldown (a provider-type failure for one run no longer
loops); such a failure counts toward the streak once it repeats with a successful config read in
between; an agent-side minimum processing budget; refund-api's budget pinned to one setting.

**Locked 2026-11-12.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Precedence:** fills in ADR-008 Part 2 (the reconciler) and ADR-004 step 5. It changes two
locked rules: **ADR-012's quiet-period guard** ("at startup:
`quiet_period > client_timeout + processing_budget + margin`, with the processing budget taken
from `GET /config`") is replaced by Part 2's per-run and current bounds; and **ADR-013's read
validation** gains a minimum processing budget (Part 2). Everything else in ADR-004, ADR-008,
ADR-010, ADR-012 and ADR-013 about reconciliation stands.

## Context
Settled already:
- **ADR-008 Part 2:** a reconciler claims `NEEDS_RECONCILIATION` runs with the workers' lease and
  fencing, **only once their quiet period has passed** (the claim's `WHERE` checks it, so it
  never holds a lease while waiting), looks the refund up by `action_id`, and **never sends**.
  Found → `confirmed`, `COMPLETED`, "refund issued". Not found → `rejected`, `ESCALATED`, "we
  couldn't confirm the refund; a person will follow up".
- **ADR-010:** a lookup error is never "not found"; two or more refunds for one action →
  `confirmed` plus an alert.
- **ADR-012:** the lookup must match the stored payload in full (`classify_lookup`).
- **ADR-004 step 5:** the quiet period is the client timeout plus the provider's maximum
  processing time, plus a margin; refund-api enforces that maximum with a server-side deadline.

Not settled:
1. **Where the reconciler runs.**
2. ★ **What the quiet period covers.** ADR-012 checks it once, at startup, against the budget
   read then: the same gap ADR-013 closed for the retry window. A budget raised later, or a
   client timeout shortened in a later agent deploy, and a run is reconciled before its last
   send could have committed: nothing found, `rejected`, capacity freed for money that then
   moves. And even a bound recorded per send misses a budget raised **between the read and the
   send**, because the last send is the one being bounded and no later read corrects it.
3. **Failed lookups**, for runs that can only leave `NEEDS_RECONCILIATION` through a lookup
   (ADR-008: a finished run can't be `unknown`), without paging for every run during one
   refund-api outage.
4. **The kill switch.** "Not found → `rejected`" is the reconciler's one decision that frees
   capacity. A refund-api bug answering `200 []` (an ignored filter, an empty replica) passes
   `classify_lookup`, since an empty list has no row to mismatch, and would reject every
   uncertain run on the next pass.

## Options Considered

### Part 1 — Where the reconciler runs

#### Option A: A second claim loop inside the worker process — CHOSEN
- How it works: `run_worker` runs two loops as tasks over the same engine, refund client, gate and
  alerter: the send loop (ADR-014) and a reconcile loop with its own claim statement, one run at a
  time each. Both use ADR-014's lease, heartbeat (`hold_claim`), fencing and releases.
- Pros: one process to deploy (the free-tier constraint); a slow send never delays reconciliation
  and the reverse; every safety mechanism is shared, already-tested code.
- Cons: a process crash stops both (each resumes from the database); the worker's settings grow.
- Cost/latency/complexity profile: one more task; one config read and one lookup per reconciled run.

#### Option B: A separate process (`python -m agent.reconciler`) — rejected
- How it works: its own entry point, scaled on its own.
- Pros: independent scaling and failure.
- Cons: a second process everywhere, for a loop that handles only the rare uncertain run.
- Why we didn't use it here: nothing about reconciling needs isolation from sending.
- When it WOULD be the better choice: high volumes of uncertain runs, or slow or expensive
  lookups that need their own capacity.

#### Option C: The worker's own claim also takes `NEEDS_RECONCILIATION` runs — rejected
- How it works: one claim statement, three statuses, dispatch by status.
- Pros: one loop.
- Cons: ADR-014's most-tested statement gains per-status conditions; uncertain runs compete with
  approved refunds for the one slot (S3).
- Why we didn't use it here: it complicates that statement for no gain.
- When it WOULD be the better choice: a design with one status-agnostic job queue.

### Part 2 — What the quiet period covers

#### Option A: The larger of a per-run recorded bound and a bound from a fresh read at reconcile time — CHOSEN
- How it works:
  - **Recorded with each send** (ADR-014 Part 3's attempt transaction, which has the read made
    just before the send and the client timeout the send runs under):
    `in_flight_bound_ms = greatest(in_flight_bound_ms, client_timeout + processing_budget)` and
    `max_client_timeout_ms = greatest(max_client_timeout_ms, client_timeout)`.
  - **Read at reconcile time:** each reconcile pass first reads `GET /config` (a fresh read, not
    the gate's cache, which can be a full refresh interval old) and feeds the gate.
  - **The claim waits for both:**
    `last_attempt_at + greatest(in_flight_bound, max_client_timeout + budget_now) + margin < now()`,
    on the database clock.
    - The recorded bound covers a budget that has since **decreased**: a request received under
      the older, larger budget is still covered.
    - The fresh read covers a budget that **increased around the send**: if refund-api was
      redeployed with a larger budget between the worker's read and its send, the request ran
      under the new one, and the reconciler now reads it.
    - Taking the largest client timeout covers an agent redeployed with a shorter one.
  - **Nothing sent, nothing in flight:** a send only ever follows its attempt record (ADR-014
    Part 3), so with `send_attempts = 0` both columns are `NULL` and only the margin applies. A
    `CHECK` keeps both `NULL` exactly when `send_attempts = 0`.
  - The margin (`RECONCILER_QUIET_MARGIN_SECONDS`) covers what ADR-004 and ADR-010 already leave to
    a margin: the gap between refund-api's deadline check and its commit, and transit time.
  - **Here the dangerous direction is "too small".** For the retry window an over-large value was
    the risk (ADR-013 caps it); here an under-reported budget shortens the quiet period, and both
    terms come from refund-api. So: ADR-013's read validation gains an agent-side minimum,
    `REFUND_CLIENT_MIN_PROVIDER_BUDGET_SECONDS`; a reported budget below it is an invalid read
    (nothing sent, the gate closes, an alert). And a refund-api test pins `GET /config`'s
    `processing_budget_ms` to the very setting its deadline enforces: one source, not two values
    that happen to match.
- Pros: each run is bounded by the settings its sends ran under **and** the provider's current
  settings, whichever is larger; no startup check to keep in step.
- Cons: two columns and a migration; one config read per pass; the claim condition is per row.
- **Residual, recorded in `KNOWN_TRADEOFFS.md`:** a budget raised between a read and a send and
  then lowered again before reconciliation escapes both terms. Like ADR-013's rolling-deploy
  limit, it needs two config changes inside one run's life; with refund-api rolling deploys,
  ADR-013 Option C (the request carrying its own bound) becomes the right design for both.
- Cost/latency/complexity profile: two `greatest()`s in an update that already runs; one small
  GET per reconcile pass.

#### Option B: ADR-012 as written: a static quiet period, checked at startup — rejected
- How it works: `RECONCILER_QUIET_PERIOD_SECONDS`, refused at startup unless above the client
  timeout plus the budget read then plus the margin.
- Pros: one setting.
- Cons: a budget raised after startup, or a client timeout lowered in a later deploy, and a run is
  reconciled before its last send could have committed.
- Why we didn't use it here: a check that runs once can't bound what changes later.
- When it WOULD be the better choice: a provider whose processing bound is fixed and documented,
  and an agent whose timeout never changes.

#### Option C: The recorded bound alone (the first draft) — rejected
- How it works: Option A without the fresh read.
- Pros: no read per pass.
- Cons: a budget raised between the worker's read and its send is recorded as the old, smaller one,
  and the last send is exactly the one nothing later corrects.
- Why we didn't use it here: the same race ADR-013 tolerated only because a later send re-reads.
- When it WOULD be the better choice: a provider whose budget can't change while it serves.

#### Option D: The budget read at reconcile time alone — rejected
- How it works: `client_timeout + budget_now + margin`.
- Pros: always current.
- Cons: wrong when the budget decreased: a request received under the older, larger budget can
  still commit after a quiet period sized by the new, smaller one.
- Why we didn't use it here: the mirror of ADR-013's bug.
- When it WOULD be the better choice: never on its own; it's half of Option A.

### Part 3 — A lookup that fails

#### Option A: Tell refund-api's failures from the run's; only the run's count toward the streak — CHOSEN
**Amended by [ADR-017](017-reconciler-streak-evidence.md):** a repeated refund-api-type failure
counts toward the streak only if refund-api answered another run's lookup in between (not merely a
config read, since `GET /config` never touches refund-api's database); the stale-unknown signal
also pages, except for runs whose latest "not found" the kill switch held.

- How it works (ADR-014 Part 4's distinction, applied to lookups):
  - **Every reconciler release applies the cooldown.** Only the streak depends on the cause.
    Status codes can't tell a refund-api outage from a refund-api bug that fails on one
    `action_id` only; without a cooldown, that run would be reclaimed the moment the gate reopened,
    in a loop that hammers refund-api, starves every other uncertain run and never pages (ADR-014
    S12's loop through another door). During a real outage the gate is closed anyway, so the
    cooldown costs only a short delay after recovery.
  - **refund-api-type failure:** no response, a 5xx or other non-200, a body that isn't strict
    JSON, a 401/403. The lookup **marks the gate stale** (no more claims by either loop until a
    config read succeeds), releases with the cooldown, and alerts; a 401/403 pages. It counts
    toward the streak **only when it repeats for the same run with a successful config read in
    between**: refund-api answered the read, so the failure is evidently this run's. A real outage
    produces no streak, because the config reads fail too. (The "failed before" mark is kept in
    the process's memory; a restart costs one extra cycle, not a missed page.)
  - **The run's failure:** a refund in the answer that doesn't match the stored payload, or an
    unexpected exception: alert, release **with** ADR-014's cooldown, and count toward
    `release_streak`. At the limit the run can't escalate (its outcome is unknown), so it **pages**
    with its `run_id` and keeps cycling. Pages are limited per `(kind, run_id)` per interval
    (ADR-014 Part 6), so a stuck run pages once per interval, not once per cooldown.
  - **ADR-004's stale-unknown alert:** each pass, an alert for every `NEEDS_RECONCILIATION` run
    older than `RECONCILER_STALE_ALERT_SECONDS`, limited per run.
- Pros: one outage is one signal; a broken run still reaches a person; reuses tested machinery.
- Cons: two release paths to keep apart; a refund-api bug that returns mismatching rows for every
  action looks run-specific, so it pages per run (rate-limited) rather than once.
- Cost/latency/complexity profile: one release per failure.

#### Option B: Every failed lookup counts toward the streak (the first draft) — rejected
- How it works: any failure releases with a cooldown and counts.
- Pros: one path.
- Cons: a refund-api outage pushes every uncertain run to the limit, and every run pages.
- Why we didn't use it here: the cause is one outage, not N broken runs.
- When it WOULD be the better choice: never with a shared dependency behind the lookup.

#### Option C: Keep the claim and retry with backoff — rejected
- How it works: hold the run and retry the lookup in the loop.
- Pros: faster after a blip.
- Cons: one bad run blocks the loop's only slot; an outage holds it throughout.
- Why we didn't use it here: the gate and the cooldown do it without blocking.
- When it WOULD be the better choice: several reconcile slots per process.

#### Option D: Alert once and stop retrying — rejected
- How it works: leave the run for a person after a failure.
- Pros: no repeated lookups.
- Cons: every blip becomes manual work, with capacity reserved until someone acts.
- Why we didn't use it here: most failed lookups succeed a minute later.
- When it WOULD be the better choice: a provider with expensive or rate-limited lookups.

### Part 4 — The kill switch

#### Option A: With the kill switch on, act on "found" but not on "not found" — CHOSEN
- How it works: the reconciler still claims and looks up. Found → `confirmed` (safe: money moved,
  and recording it only reserves capacity correctly). Not found → no decision: release with the
  cooldown, **without** a streak increment (an admin's switch, not the run's fault). When the
  switch is off again, the next pass decides normally.
- Pros: the incident switch (ADR-009: "refund-api misbehaving") now covers the reconciler's one
  capacity-freeing decision; confirmations keep flowing.
- Cons: one more condition; uncertain runs pile up while it's on (intended).
- Cost/latency/complexity profile: one check per not-found.

#### Option B: Ignore the kill switch, since the reconciler never sends (the first draft) — rejected
- How it works: the reconciler runs regardless.
- Pros: simplest.
- Cons: a `200 []` bug rejects every uncertain run on the next pass, freeing capacity and telling
  customers no refund was confirmed, during exactly the incident the switch is for.
- Why we didn't use it here: "never sends" isn't "never harms".
- When it WOULD be the better choice: a reconciler that only ever confirms.

#### Option C: Stop the reconciler entirely while the switch is on — rejected
- How it works: no claims at all.
- Pros: simplest pause.
- Cons: confirmations stop too, so capacity stays reserved for refunds that really happened.
- Why we didn't use it here: only "not found" is dangerous.
- When it WOULD be the better choice: if lookups themselves were unsafe during incidents.

### Smaller decisions
- **S1. The reconcile claim:** ADR-014's pattern (one `UPDATE` over a `SKIP LOCKED` subselect,
  conditions repeated in the outer `WHERE`) with its own named conditions: status
  `NEEDS_RECONCILIATION`, lease free, cooldown passed, quiet period passed (Part 2, with the
  budget just read as a parameter); oldest `last_attempt_at` first. No R14 hold filter (it holds
  sends); the kill switch acts on the decision instead (Part 4).
- **S2. The gate:** the reconcile loop claims only while ADR-012's gate is open, and its fresh read
  (Part 2) feeds the gate like any other read.
- **S3. Outcomes:** found → ADR-014's `confirm` exit ("refund issued"), plus a `duplicate_refunds`
  alert when there are two or more (ADR-010). Not found (switch off) → `rejected`, `ESCALATED`,
  `escalated_at` kept, the `not_confirmed` reply. Both clear the claim and bump `claim_version`,
  as every exit and every release does (ADR-014 S9, as locked: "every release bumps the version").
- **S4. Migration 0003:** `in_flight_bound_ms` and `max_client_timeout_ms` (`BIGINT`, `> 0`, both
  `NULL` exactly when `send_attempts = 0`); it refuses a database with sent runs that lack them,
  as 0002 did, rather than guess.
- **S5. The release paths:** every reconciler release applies the cooldown; a cooldown no longer
  implies a streak increment, and the caller says whether it counts (Part 3's run failures and
  repeated provider-type failures do; Part 4's held not-found and a first provider-type failure
  don't).
- **S6. A rule for every loop from here on:** any release after which the same run could be
  claimed again immediately must apply a cooldown. It has now been needed three times (ADR-014
  S12, ADR-016 Part 3 twice); the next loop gets it by default.

## Decision
Part 1 A, Part 2 A, Part 3 A, Part 4 A, S1–S5.

### Tests (each with a negative control)
| Test | Negative control |
|---|---|
| A run inside its quiet period isn't claimed; after it, it is | Without the quiet condition, claimed at once |
| The recorded bound is the largest: an earlier send under a larger budget keeps the run waiting | Recording only the last send's bound claims it early |
| A budget raised between the read and the send (through a seam): the reconciler's fresh read waits for the new budget | The recorded bound alone claims it early |
| A budget lowered after the send: the recorded bound still waits for the old one | The fresh read alone claims it early |
| A client timeout lowered in a later deploy: the recorded maximum still covers the old one | The current timeout claims it early |
| A run with no recorded send waits only the margin | (The `CHECK` refuses a bound without a send) |
| Found → `COMPLETED`/`confirmed`, reply; two refunds → also an alert | A planted "found = not found" sets `rejected` |
| Not found after the quiet period → `ESCALATED`/`rejected`, `not_confirmed` reply, capacity freed | — |
| A refund-api outage with ten uncertain runs: no streak increments, the gate closes, at most one page | Counting transport failures pages for every run |
| refund-api returns 500 for one `action_id`'s lookup only: the other uncertain runs are reconciled, and the failing run reaches the streak limit and pages | A release without the cooldown reclaims it in a loop (bounded wait fails) |
| A reported processing budget below the agent's minimum is an invalid read: no claims, an alert | Accepting it shortens the quiet period |
| refund-api: `GET /config`'s `processing_budget_ms` and the deadline come from the same setting | Two settings that drift apart are caught |
| A mismatched refund counts toward the streak; at the limit the run pages, keeps its status, once per interval | A streak that moves it breaks ADR-008's `CHECK` |
| Kill switch on and an empty lookup: the run stays `NEEDS_RECONCILIATION`, no streak; switch off: `rejected` | Ignoring the switch rejects it |
| Kill switch on and a found refund: `confirmed` | (Confirmations aren't held) |
| The reconciler never sends: the fake client fails any `send` | — |
| Stale-unknown alert after the threshold, once per run per interval | No alert below the threshold |
| **e2e:** `slow_commit` — the agent gives up, refund-api commits later; the reconciler waits out the quiet period and finds it → `confirmed`, one refund | A zero quiet period finds nothing and sets `rejected` |
| **e2e (ADR-010):** attempt 1 commits, a malformed retry gets `invalid_request` → `NEEDS_RECONCILIATION` → `confirmed`, never `rejected` | Mapping `invalid_request` to rejected ends `rejected` |

## Consequences
- Migration 0003 (`in_flight_bound_ms`, `max_client_timeout_ms`); the attempt transaction records
  both.
- `release_claim`'s cooldown and streak become separate choices (S5).
- New settings: `RECONCILER_QUIET_MARGIN_SECONDS`, `RECONCILER_STALE_ALERT_SECONDS`,
  `REFUND_CLIENT_MIN_PROVIDER_BUDGET_SECONDS`.
- The worker process runs two loops; its readiness log names both.
- KNOWN_TRADEOFFS: a run that never resolves cycles and pages; the raise-then-lower residual
  (Part 2); transit time beyond the margin is still an assumption (ADR-010); **"not found" assumes
  refund-api's lookups are strongly consistent** (a read from a lagging replica would make it
  unreliable); a refund-api bug returning mismatched rows for every action pages per run.
- Pointer lines into ADR-008 (the reconciler), ADR-012 (the quiet-period guard replaced) and
  ADR-013 (the minimum budget in its read validation).
- **Revisit if:** uncertain runs need their own process (Part 1 B); refund-api runs rolling
  deploys (then requests carry their own bound, ADR-013 Option C); refund-api's lookups move to a
  replica (then "not found" needs a consistency guarantee first).

## Interview-ready summary
"My reconciler settles refunds whose outcome is unknown: it waits out a quiet period, looks the
refund up, and never sends. The quiet period is where the bugs hide. A check at startup can't see
later config changes, so each send records the longest it could take, and that still isn't enough:
if the provider's budget goes up between my config read and my send, the request runs under a
budget I never saw. So the reconciler waits for the larger of what the sends recorded and what the
provider says now. Each bound comes from the settings the request actually ran under, or from the
provider's current settings, whichever is larger, because a config change can land between my read
and my send. Two more things came out of review: one refund-api outage mustn't page for every
uncertain run, so provider failures close the gate instead of counting against each run; and the
kill switch now stops the one decision that frees capacity, rejecting on 'not found', because a
provider bug returning an empty list would otherwise reject every uncertain refund at once."

# ADR-018: Provider Breakers, One per Path, Judged on Real Request Outcomes

**Status:** Accepted (2026-11-13). Revised the same day after two outside reviews. First: two
breakers, one per path (a shared one let a lookup reopen the send path during a write outage, and
its evidence counter brought back ADR-017's bug); probes chosen to spare a run's attempts; a probe
claim that sends nothing frees its slot; breakers start half-open; any answer that isn't a
well-formed refund-api answer counts as a failure. Second: probe with the run that costs least to
lose (one already doomed first) and state the bound; no per-run stale pages while the read
breaker is open; renamed "provider breaker" throughout, because "breaker" already means ADR-009's
policy breaker.

**Locked 2026-11-13.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Naming:** "provider breaker" (write or read) in this ADR, its code (`agent/worker/provider_health.py`),
settings (`PROVIDER_BREAKER_*`), gate reasons and log kinds (`provider_write_open`,
`provider_read_open`). ADR-009's breaker (R13, `breaker_tripped_at`, admin reset) is a policy
control on refund volume and is unrelated.

**Precedence:** amends ADR-012 Part 4 and ADR-014 Part 4 (what stops claiming), ADR-014 Part 3
(what happens between attempts), ADR-016 S2 and Part 3 (the reconciler claims only while its
breaker allows; a refund-api-type lookup failure feeds the read breaker instead of closing the
shared gate) and ADR-017 (its evidence counter moves into the read breaker, unchanged in meaning).
Everything else in them stands: config reads still validate every send (ADR-013) and still close
the gate when they fail; the age check still bounds every send; the reconciler still never sends.

## Context
Found while reviewing ADR-017. The claim gate is fed only by `GET /config` reads (ADR-012 Part 4,
ADR-013): the interval refresh, each per-send read and each reconcile pass. refund-api's
`/config` is built once at startup and never touches its database
(`services/refund_api/src/refund_api/app.py`, `get_config`). **With refund-api's database down:**
1. config reads succeed, so the gate stays open and the worker keeps claiming `APPROVED` runs;
2. each send gets a fast 5xx (a pool timeout answers 503 `deadline_exceeded`; nothing commits),
   which is a retry (`classify_send`); five attempts run out in about 8 s and the run moves to
   `NEEDS_RECONCILIATION` (`execute.py`, `retries_exhausted`);
3. after recovery each lookup correctly finds nothing, so each run ends `rejected`/`ESCALATED`
   with "a person will follow up".

**Every refund approved during the outage is escalated to a person.** No money is lost, and each
step is individually correct; the failure is that the agent never learns refund-api is down,
because the only health signal it trusts is a static endpoint. Real requests (sends and lookups)
are the only signal that exercises refund-api's dependencies.

Constraints: one worker process today (free tier), two loops in it (ADR-016 Part 1); refund-api
is a separate service the agent can't see into (ADR-007); a run whose outcome is unknown can
only be settled by a lookup or a person (ADR-008); every release that could be reclaimed at once
applies a cooldown (ADR-016 S6).

## Options Considered

### Option A: Two in-process provider breakers, one per path, fed by that path's real requests — CHOSEN
- How it works:
  - **A write breaker fed by sends gates the send loop; a read breaker fed by lookups gates the
    reconcile loop.** Each judges refund-api by its own path's traffic, because a read succeeding
    says nothing about writes and the reverse (a lock on `charges` fails sends while lookups work;
    an index problem on `refunds` can fail lookups while sends commit). A full outage trips both,
    at the cost of one threshold's worth of failures each.
  - **What counts** (S6): a failure is no response, any 5xx, or any answer that isn't a
    well-formed refund-api answer for that endpoint: a status outside its documented set (from
    `contracts/refund_api.openapi.json`) or an error body that isn't strict JSON. A success is any
    well-formed answer: for sends a 201, a business 400 or a 422; for lookups a 200 with a
    well-formed list (found, not found or a mismatched row). 409 and 429 are neither (in-flight
    key, backpressure). 401/403 stay with the gate, which pages on them.
  - **Closed → open** after `PROVIDER_BREAKER_FAILURE_THRESHOLD` failures in a row on that path. **Open:**
    that path's loop doesn't claim; a send loop's `EXECUTING` run between attempts stops and is
    released (S2).
  - **Half-open** after a backoff (`PROVIDER_BREAKER_OPEN_SECONDS`, doubling up to
    `PROVIDER_BREAKER_MAX_OPEN_SECONDS`): one claim is let through (S3), and its next request on that path
    is the probe. Success closes the provider breaker and resets the backoff; failure re-opens it with the
    next, longer backoff; a probe claim that ends without that request frees the slot (S4).
  - **Both start half-open** (S5).
  - **Config reads feed neither** (they can't see the outage these exist for), and neither feeds
    them: a failed config read still closes the gate (ADR-014 Part 4).
- Pros: each path judged by its own traffic, so a working read can't reopen a broken write path
  and ADR-017's principle holds; an outage pauses the affected loop after a few failures instead
  of escalating every approved refund; recovery proven by a real request; in-process, no schema
  change, no change to refund-api's production code.
- Cons: per process, so several workers each learn an outage separately; a probe is a real
  request (a send's attempt is counted and may commit, S3); a degraded refund-api can pause every
  refund on that path; two thresholds' worth of failures in a full outage.
- Cost/latency/complexity profile: two counters, states and timers per process; one check before
  each claim and each attempt.

### Option B: One provider breaker shared by both paths (the first draft) — rejected
- How it works: Option A with a single provider breaker fed by sends and lookups alike; its success count
  is ADR-017's evidence.
- Pros: one mechanism; a full outage trips it after one threshold's worth of failures.
- Cons: during a **write-only** outage a half-open probe that happens to be a reconciler lookup
  succeeds, closes the provider breaker and resets its backoff; sends fail again and re-open it at the
  base backoff; the next lookup closes it again. Each cycle burns a threshold's worth of send
  attempts, so runs drain into reconciliation and escalation, more slowly. And with a 201 counted
  as evidence, a **read-only** outage moves ADR-017's counter, so every lone lookup failure counts
  toward its run's streak and pages per run: ADR-017's bug, reintroduced.
- Why we didn't use it here: a success on one path is no evidence about the other.
- When it WOULD be the better choice: a provider with a single endpoint, or one whose reads and
  writes provably share every dependency.

### Option C: Provider-breaker state in the agent's database, shared by every worker — rejected
- How it works: Option A's two breakers' state in a one-row table (like `policy_controls`), updated by any
  worker's outcomes, read by every claim statement.
- Pros: every worker learns the outage at once; survives restarts; visible to an operator.
- Cons: a database write per failure on the hot path; a migration and role grants; contention on
  one row during exactly the incident it tracks; more failure modes (the provider-breaker row unreadable).
- Why we didn't use it here: one worker process today, so there is nothing to share.
- When it WOULD be the better choice: several worker processes or hosts, where per-process
  learning multiplies the failures spent before every worker pauses.

### Option D: Make `GET /config` probe refund-api's database — rejected
- How it works: `/config` runs a query, so a database outage fails config reads and the existing
  gate closes (ADR-017 Option B, for the send path).
- Pros: no new mechanism in the agent; both loops pause through the gate they already have.
- Cons: a change to the other service and its contract; every send's config read now depends on
  refund-api's database latency; it probes `SELECT 1`, not the refund path, so a failure only
  sends hit (a lock on `charges`, a bad plan, a full disk on one table) still escalates every run.
- Why we didn't use it here: it proves the database answers, not that refunds work.
- When it WOULD be the better choice: a provider with a documented readiness endpoint that
  covers its write path.

### Option E: Don't count provider-type send failures as attempts — rejected
- How it works: keep the attempt budget for "refund-api answered and we must retry"; a 5xx or no
  response backs off and retries until the run's own send window (ADR-013) ends it.
- Pros: no new state; each run waits out a short outage by itself.
- Cons: the worker has one slot (ADR-014 S3), so one run retrying for its whole window blocks every
  other run; every claimed run keeps hitting refund-api during the outage; a "no response" may
  have committed, so treating it as "not an attempt" breaks ADR-012's worst-case arithmetic; an
  outage longer than the window still escalates everything.
- Why we didn't use it here: it trades mass escalation for a stalled worker and unbounded load.
- When it WOULD be the better choice: many send slots and outages known to be shorter than the
  window.

### Option F: Let the reconciler re-send runs whose every attempt failed provider-type — rejected
- How it works: "not found" after only 5xx/no-response attempts re-queues the run for sending.
- Pros: refunds approved during an outage eventually go through without a person.
- Cons: breaks ADR-008's rule that the reconciler never re-sends; "no response" is not "not sent":
  the quiet period makes a late commit unlikely, not impossible (transit beyond the margin is an
  assumption, ADR-010), and by then the key may have expired, so a re-send could refund twice;
  it turns a lookup into a send decision.
- Why we didn't use it here: it reopens the money question ADR-008 closed.
- When it WOULD be the better choice: never, while keys expire and outcomes can be unknown.

### Option G: Accept it; document the mass escalation — rejected
- How it works: as today; a person handles every refund escalated during an outage.
- Pros: no work.
- Cons: the exact outcome the gate exists to prevent; the escalation queue fills during every
  database incident.
- Why we didn't use it here: it is a real production failure mode with a known fix.
- When it WOULD be the better choice: a demo with no outage expectations.

### Smaller decisions
- **S1. Trip before a full-budget run runs out:** `PROVIDER_BREAKER_FAILURE_THRESHOLD` must be below
  `REFUND_CLIENT_MAX_ATTEMPTS` (refused at startup otherwise), so a run that starts with its full
  budget and trips the write breaker by itself still has an attempt left when it checks the
  breaker, and is released instead of reconciled. This protects only runs with a full budget: a
  run resumed with most of its attempts already used (say four of five) can still reach
  reconciliation before the provider breaker opens. With the threshold at or above the attempt count, the
  first run of every outage would escalate.
- **S2. An `EXECUTING` run when the write breaker opens:** checked between attempts, before the
  next attempt's config read. Open → release **with the cooldown and without a streak increment**
  (refund-api's fault, not the run's; ADR-016 S5/S6), status `EXECUTING`, attempts as recorded.
  Nothing more is sent, so nothing more is counted. It resumes when the provider breaker closes; its age
  check (ADR-013) still ends it if the outage outlasts its window. An attempt already on the wire
  when the provider breaker opens finishes.
- **S3. The probe is the run that costs least to lose:** a write probe that fails still uses an
  attempt, and a probe on an `APPROVED` run also starts its send window (`first_send_started_at`,
  ADR-013). In half-open the write breaker claims, in order:
  1. an `EXECUTING` run that still passes its age check but whose window ends before the next
     possible probe (remaining window, measured on the run's snapshot, under the current backoff):
     it can't be saved by waiting, so probing with it costs nothing extra;
  2. otherwise an `APPROVED` run (its first send has the full budget);
  3. otherwise an `EXECUTING` run, fewest attempts used first.
  Claimed normally (closed), the order stays ADR-014's (`EXECUTING` first). Without this order,
  the run released by S2 is the next probe once the backoff outgrows its cooldown, and a long
  outage drains it, then the next run, into reconciliation one probe at a time. **The bound:** at
  most one run lost per failed probe, and fewer while the backoff is short against the window (a
  run started by a failed probe is then probed again inside its own window, as the doomed run, so
  one run lasts many probes). Because the backoff doubles, probes grow roughly logarithmically with
  the outage's length up to `PROVIDER_BREAKER_MAX_OPEN_SECONDS`, then one per maximum interval. So
  that maximum trades recovery speed against this cost: kept below the provider's window minus the
  client timeout (with the default 600 s window, under about 590 s), a probe-started run gets at
  least one more probe inside its window. A probe is a normal request: the attempt is recorded
  and counted (it may commit). The read breaker's probe is the next reconcile claim (lookups cost
  no attempts and start no window).
- **S4. A probe claim that ends without a request frees the half-open slot:** pre-send can end
  `HOLD`, `OUT_OF_POLICY` or `55P03`, a config read can fail, a reconcile claim can find no
  payload, any claim can be lost. None sent the probe, so the slot is freed and the next claim may
  probe; the backoff is unchanged. A probe answered with a neutral status (409, 429) frees it too.
  Otherwise the provider breaker would sit half-open with a probe that never happens.
- **S5. Start half-open:** both breakers live in memory, so a worker restarted during an outage
  would start closed and spend a threshold's worth of attempts finding out again, on every restart
  under a supervisor. Half-open at start makes the first request a probe; with refund-api healthy
  it closes at once and costs nothing (each loop has one slot anyway, ADR-014 S3).
- **S6. Infrastructure errors count:** a misrouted URL or a broken ingress answers 404 or an HTML
  502 for everything. ADR-012 still classifies an unrecognized send status as `RECONCILE` (it says
  nothing about this action), so up to a threshold's worth of runs reconcile before the write
  breaker opens; after that nothing more is sent. The documented status sets come from the
  OpenAPI contract the agent already tests against (ADR-012 contract test), so a status refund-api
  doesn't send is a failure by definition.
- **S7. ADR-017's evidence is the read breaker's success count:** the count of lookups refund-api
  answered moves into the read breaker, unchanged in meaning; a send's 201 is **not** evidence,
  so ADR-017's principle (test the path that failed) holds. ADR-017's per-run marks, the cooldown
  on every release and S3/S4's stale page stay as they are. A refund-api-type lookup failure feeds
  the read breaker and no longer closes the shared gate (ADR-016 Part 3's `lookup_failed()`),
  which would also have stopped the send loop.
- **S8. Signals:** each provider breaker opening alerts (one key per path, rate-limited); a provider breaker open
  past `PROVIDER_BREAKER_PAGE_AFTER_SECONDS` pages once per interval (every refund on that path is paused);
  closing logs. The readiness log names both breakers' states with the gate's.
- **S9. No per-run stale pages while the read breaker is open:** the reconcile loop stops, so
  every uncertain run ages past `RECONCILER_STALE_PAGE_SECONDS` and ADR-017 S3 would page for each
  one: the one-page-per-run outage again, by another route. While the read breaker is open, the
  per-run stale page is skipped for every run **except** one whose last outcome was its own
  failure (a mismatched row, no payload, an unexpected error; kept in the same in-memory record
  as ADR-017's marks); the read breaker's page (S8) stands for the rest, and the per-run alert
  still fires. Skipping only runs marked with a refund-api-type failure would not do: runs that
  became uncertain during the outage were never looked up, carry no mark, and would page one by
  one. Because the provider breaker's page replaces theirs, `PROVIDER_BREAKER_PAGE_AFTER_SECONDS` must be
  below `RECONCILER_STALE_PAGE_SECONDS` (refused at startup otherwise).

## Decision
Option A, S1–S9.

### Tests (each with a negative control)
| Test | Negative control |
|---|---|
| Write outage (config reads succeed, sends 5xx): after the threshold no send-loop claim; runs approved meanwhile stay `APPROVED`; none reaches `NEEDS_RECONCILIATION` through retries | Without the write breaker every claimed run ends `NEEDS_RECONCILIATION`, then `rejected` |
| The run whose failures trip the write breaker is released `EXECUTING` with the cooldown, streak unchanged, and completes after recovery with one refund | Reconciling it on open escalates it |
| Threshold at or above the attempt count is refused at startup | Accepting it lets a full-budget run exhaust its attempts |
| Write-only outage with uncertain runs whose lookups succeed: the reconcile loop keeps confirming, and no lookup closes the write breaker or resets its backoff | One shared breaker: a lookup probe closes it and sends drain into reconciliation |
| Read-only outage (lookups fail, sends succeed): sends keep completing; lone lookup failures don't count toward streaks | Counting 201s as ADR-017 evidence streaks and pages per run |
| A long write outage with several probes: no single run is drained into reconciliation by probing alone | `EXECUTING`-first probing reconciles the released run |
| A long write outage with one doomed `EXECUTING` run and several `APPROVED` runs: the doomed run is probed first, and no `APPROVED` run starts its window on a probe while a doomed one is available | `APPROVED`-first probing starts a new window on every probe |
| Read outage with ten uncertain runs past the stale page threshold: one read-breaker page, no per-run stale pages (their alerts still fire); a run whose last outcome was its own failure keeps its per-run page | Skipping only refund-api-marked runs pages for every run never looked up; skipping every run silences the run-specific one |
| `PROVIDER_BREAKER_PAGE_AFTER_SECONDS` not below `RECONCILER_STALE_PAGE_SECONDS` is refused at startup | — |
| A half-open probe claim that ends without a send (HOLD, `OUT_OF_POLICY`, a failed config read) frees the slot; the next claim probes | Keeping the slot sits half-open with no probe |
| Half-open: exactly one claim per path goes through; success closes, failure re-opens with a doubled, capped backoff | Two claims in half-open send two probes |
| A new process starts both breakers half-open: its first request on each path is a probe | Starting closed spends a threshold's worth of attempts after a restart |
| 409, 429, business 400s, 422 and 201s don't open the write breaker; failures interleaved with a success don't | Counting 409s opens it during an in-flight key |
| A 404 or a non-JSON 502 counts as a failure (S6): after the threshold nothing more is sent | Not counting them reconciles every run |
| Config reads never open or close a provider breaker; a failed config read still closes the gate | Feeding config reads to a provider breaker keeps it closed through the outage |
| Open past `PROVIDER_BREAKER_PAGE_AFTER_SECONDS` pages once per interval per path; each opening alerts once | No page below the threshold |
| **e2e:** refund-api's test build fails every send and lookup for the test's own `action_id`s while `/config` answers: approved runs wait, then complete after the fault is removed; none is escalated | Provider breakers disabled: the same scenario escalates every run |

## Consequences
- A new in-process component (`agent/worker/provider_health.py`, one instance per path) consulted by both
  claim loops and by the send loop between attempts; the claim gate's `closed_reason()` reports a
  loop's breaker; the write breaker's half-open claim uses its own ordering (S3).
- ADR-016 Part 3's `gate.lookup_failed()` goes; the read breaker takes its place (S7).
- ADR-017 S3's per-run stale page gains a second exception (S9); the in-memory record keeps each
  run's last own failure.
- New settings: `PROVIDER_BREAKER_FAILURE_THRESHOLD`, `PROVIDER_BREAKER_OPEN_SECONDS`, `PROVIDER_BREAKER_MAX_OPEN_SECONDS`,
  `PROVIDER_BREAKER_PAGE_AFTER_SECONDS` (shared by both breakers).
- refund-api's test build gains a **per-action** fault mode that fails every request for its
  `action_id` while it is registered, so tests stay isolated (ADR-010 Part 3); `/config` is
  unaffected by design.
- `KNOWN_TRADEOFFS.md`: per-process learning (Option C's gap); a degraded refund-api can pause
  every refund on a path; a probe is a real, counted request; each failed write probe can cost one
  run its send window (S3's bound: at most one per failed probe, logarithmic in the outage's
  length up to the maximum backoff, and `PROVIDER_BREAKER_MAX_OPEN_SECONDS` trades recovery speed
  against it); S1 protects only full-budget runs;
  an unrecognized status still reconciles up to a threshold's worth of runs (S6); an outage longer
  than a run's window still ends in reconciliation for runs already `EXECUTING`.
- Pointer lines in ADR-012, ADR-014, ADR-016 and ADR-017 once locked.
- **Revisit if:** more than one worker process runs (Option C); refund-api gains a readiness
  endpoint covering its write path (Option D); outages are routinely longer than the send window.

## Interview-ready summary
"I found that my health signal was a static endpoint, so it couldn't see the failures that
mattered: with the provider's database down, every config read passed, every send failed fast,
and every refund approved during the outage would have been escalated to a person. The fix was to
judge provider health by real request outcomes, which is what a circuit breaker does, and to judge
each path by its own traffic, because a read succeeding tells you nothing about writes: my first
draft let a successful lookup reopen the send path during a write outage. Two details mattered.
The threshold has to be below the per-run attempt count, or the run that trips the write breaker has
already run out. And the probe has to come from the run that costs the least to lose: first one that's already
doomed, then one with the most budget left, or a long outage drains one run after another into
reconciliation, one probe at a time. I also renamed it 'provider breaker', because 'breaker'
already meant the policy breaker, and 'the provider breaker is open' at 3 a.m. has to mean one thing."

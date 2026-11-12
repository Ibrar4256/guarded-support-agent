# ADR-014: The Worker — Process, Claiming, the Attempt Budget and Failed Config Reads

**Status:** Accepted (2026-11-12). Revised the same day after outside review: a failed
per-send read closes the claim gate (no claim/release loop), the first send's budget stated,
Part 4 added to the precedence list, a claim index, rate-limited alerts, and releases that
expect the failure to recur wait out a cooldown. Second pass: the cooldown is a
`claimable_after` column, not a borrowed lease; a persisted `release_streak` stops a run that
fails on every claim; every release bumps `claim_version`.

**Locked 2026-11-12.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Precedence:** for the worker's behavior this ADR fills gaps in ADR-004 step 7,
ADR-005, ADR-009, ADR-012 Parts 3–4 and ADR-013. It changes no rule they set, with three narrow
exceptions, each stated where it applies:
- **Part 3** tightens ADR-012/013's per-send order: the age check and the fenced attempt
  update become **one transaction**, and the send shares that transaction's time budget
  instead of starting a fresh `client_timeout`.
- **Part 4** corrects ADR-013's statement that after a failed read "the run keeps aging, so a
  long failure ends in the age check": the age check needs a window that was read, so
  termination comes from the run's own snapshot, a bounded number of failed reads, and the
  claim gate closing.
- **Smaller decision S1** stores the window snapshot as `runs.send_window_ms` (integer
  milliseconds), where ADR-012 wrote `send_window_seconds`.

## Context
Step 2 builds the worker: it claims `APPROVED` and `EXECUTING` runs, runs the pre-send
policy check once at the move to `EXECUTING`, sends with bounded retries, and records the
outcome. The locked ADRs settle most of it:

| Settled | Where |
|---|---|
| Claim compare-and-set, statuses, lease on the database clock, `claim_version` fencing on every write, heartbeat as its own task, lease ≥ 3× heartbeat, renewal stops when a step overruns, a 0-row heartbeat stops everything | ADR-004 step 7, ADR-008 |
| Policy check once, in the customer-locked transaction that moves `APPROVED → EXECUTING`; `55P03` handling; never re-checked on resume | ADR-004 step 6, ADR-005 |
| `HOLD` releases the claim, `OUT_OF_POLICY` escalates, `FAIL_CLOSED` uses a separate fenced transaction; held runs aren't claimed | ADR-009 |
| Per-send order read → age check → fenced update → send; a failed read sends nothing and counts nothing; 401 releases, pages; the cache only gates claiming | ADR-012, ADR-013 |
| Fault hooks are injected seams (no-op in production), implemented in `agent_testing` | ADR-007 |

Planning the code found five things they don't settle. Two of them are gaps in the safety
argument, not just choices:

1. **Where the worker runs** (its own process or inside the API).
2. **How it finds work** among many runs and many workers.
3. ★ **The attempt budget.** ADR-012/013 order the age check, the fenced update and the send
   as separate steps, and `HttpRefundClient` starts a fresh `client_timeout` when `send()` is
   called. So an attempt can end up to *(update duration + client_timeout)* after the age
   check, and `last_attempt_at` is written before the send starts. The age check and the
   reconciler's quiet period both assume an attempt ends within `client_timeout` of those
   points. Under row-lock contention or a slow commit, it doesn't.
4. ★ **Failed config reads don't terminate.** ADR-013 says a failed read backs off and "the
   run keeps aging, so a long failure ends in the age check". But the age check needs a
   window that was read. If reads keep failing, the worker never reaches it, and holds the
   claim (heartbeating fine) for as long as refund-api is down. An `APPROVED` run doesn't age
   at all.
5. **How a test process installs the fault hooks** for the worker (ADR-007 says where they
   live, not how a separate worker process gets them).

Plus the alert/page channel, which several ADRs require and nothing provides yet.

## Options Considered

### Part 1 — Where the worker runs

#### Option A: Its own process, same package and image, own entry point — CHOSEN
- How it works: `python -m agent.worker` runs `run_worker(settings, hooks)`. Same `agent`
  package and Docker image as the API, separate process. Whether it gets its own machine or
  shares a container with the API is the deployment ADR's call.
- Pros: Crash tests (`os._exit`, ADR-004) kill only the worker, which is exactly the failure
  they test. The API's readiness can't depend on refund-api by construction (ADR-012 Part 4).
  Scales separately (more workers ≠ more API replicas).
- Cons: A second process to run in compose, CI and deploy. On some free tiers a background
  worker costs extra (Render's free plan has no workers), which pushes the deploy toward
  Fly.io process groups or one container running both.
- Cost/latency/complexity profile: $0 locally; one compose service.

#### Option B: An asyncio task in the API's lifespan — rejected
- How it works: FastAPI starts the worker loop on startup.
- Pros: One process, one deploy; free on any host.
- Cons: A crash test's `os._exit` kills chat too. A stuck worker coroutine or a CPU-heavy
  step degrades customer latency. Every API replica becomes a worker, so worker count is
  tied to web traffic.
- Why we didn't use it here: It can't produce "the worker died, the API didn't".
- When it WOULD be the better choice: A demo-only deployment where cost is the only
  constraint; even then as a deploy option over Option A's `run_worker()`, not a different
  design.

#### Option C: A task queue (Celery, arq, Dramatiq) over Redis — rejected
- How it works: Approval enqueues a job; workers consume it.
- Pros: Mature retries, scheduling and monitoring.
- Cons: A second source of truth: the run row already *is* the queue (ADR-008), and the
  job and the run can disagree after a crash. Queue-level retries and visibility timeouts
  duplicate, and fight, the lease, fencing and age check. Adds Redis.
- Why we didn't use it here: Our safety lives in the run row; a broker can't see it.
- When it WOULD be the better choice: Many job types with no per-job database state
  machine, e.g. sending emails or generating reports.

#### Option D: A Postgres job-queue library (procrastinate, pgqueuer) — rejected
- How it works: Jobs in Postgres tables, claimed with `SKIP LOCKED`, managed by the library.
- Pros: No new infrastructure; transactional enqueue.
- Cons: Still a second table of truth beside `runs`, and its lock/retry semantics are the
  library's, not ADR-004's. A new dependency at the most safety-critical point.
- Why we didn't use it here: The claim query is three lines; the library would hide them.
- When it WOULD be the better choice: Several job types sharing one Postgres, none of
  which needs custom fencing.

### Part 2 — How the worker finds work

#### Option A: Poll with one claim statement (`SKIP LOCKED` subselect, ADR-004's conditions repeated in the outer `WHERE`) — CHOSEN
- How it works: Every `poll_interval` (config), while the config gate is open:
  ```sql
  UPDATE runs SET worker_id = :me, lease_expires_at = now() + :lease,
                  claim_version = claim_version + 1
   WHERE id = (SELECT r.id FROM runs r, policy_controls c
                WHERE <claimable: status, free/expired lease, cooldown passed (S12),
                       R14 hold filter>
                ORDER BY (r.status = 'EXECUTING') DESC, r.approved_at
                LIMIT 1 FOR UPDATE OF r SKIP LOCKED)
     AND <claimable: the same conditions again>
  RETURNING ...
  ```
  The outer `WHERE` repeats ADR-004's conditions, so the compare-and-set stays the guarantee
  and `SKIP LOCKED` only stops workers queueing on the same row.
- Pros: One round trip; workers never contend for the same row. It's still ADR-004's
  compare-and-set. Touches only the run row (ADR-005 lock order).
- Cons: Polling latency up to `poll_interval` (seconds; irrelevant for refunds) and an idle
  query per interval per worker.
- Cost/latency/complexity profile: One indexed query per poll.

#### Option B: Read candidates, then compare-and-set each id (ADR-004 literally) — rejected
- How it works: `SELECT id … LIMIT n`, then the ADR-004 `UPDATE … WHERE id = :id AND …`.
- Pros: The ADR's exact text.
- Cons: Every worker reads the same candidates and races for the same first row; most
  claims affect 0 rows. Correct but wasteful.
- Why we didn't use it here: Option A is the same compare-and-set without the herd.
- When it WOULD be the better choice: A single worker.

#### Option C: `LISTEN/NOTIFY` wake-ups with a polling fallback — rejected
- How it works: Approval `NOTIFY`s; idle workers `LISTEN`.
- Pros: Near-instant pickup.
- Cons: Needs a direct, unpooled connection per worker (ADR-005: PgBouncer transaction mode
  doesn't support `LISTEN`); the fallback poll is still required for leases expiring.
- Why we didn't use it here: Seconds of latency don't matter for a refund.
- When it WOULD be the better choice: Latency-sensitive jobs (sub-second pickup).

#### Option D: Session advisory locks as ownership — rejected
- How it works: `pg_try_advisory_lock(run_id)` held for the run's processing.
- Pros: Released automatically if the connection dies.
- Cons: Tied to one connection for the whole run (breaks with pooling, holds a connection
  across HTTP); ownership isn't visible in the row, so fencing has nothing to compare.
- Why we didn't use it here: It replaces ADR-004's lease and fencing instead of using them.
- When it WOULD be the better choice: Short critical sections inside one transaction (as
  refund-api's 409 lock does, ADR-010).

### Part 3 — One attempt, one budget

#### Option A: The age check and the attempt record in one transaction, and the send inside that transaction's budget — CHOSEN
- How it works, per attempt, after the config read (ADR-013 step 1). **Every send goes
  through this path, the first included:** the first send's transaction is a second, short
  transaction after the customer-locked `APPROVED → EXECUTING` one, and its deadline starts
  after that one commits, so lock waits in the policy transaction never eat into it.
  1. Take `deadline = monotonic() + client_timeout` **before** opening the transaction.
  2. One transaction, run row only:
     `SELECT now() − first_send_started_at, send_attempts, send_window_ms … WHERE id = :id
     AND claim_version = :v AND status = 'EXECUTING' AND lease_expires_at > now() FOR
     UPDATE`. 0 rows → lost claim, stop.
  3. The pure core decides: `send_attempts < attempts` **and** `send_allowed(elapsed,
     client_timeout, snapshot, window_read)`.
     - Yes → `send_attempts + 1`, `last_attempt_at = now()`, and
       `lease_expires_at = greatest(lease_expires_at, now() + client_timeout)`, so the lease
       outlives the attempt and no other worker can claim mid-attempt. Commit.
     - No → `EXECUTING → NEEDS_RECONCILIATION` (ADR-008: `escalated_at`, alert, claim
       cleared). Commit. Nothing sent.
  4. Send with whatever is left of `deadline` (the client takes it as a parameter). If
     nothing is left, don't send: the attempt was counted, which is the safe direction.
- Why it closes the gap: `now()` is taken after `deadline` started, so the attempt ends by
  `now() + client_timeout` in real time, where `now()` is both the age check's clock reading
  and `last_attempt_at`. The age check and the quiet period then hold as written.
- Pros: The check and the record can't drift apart; one clock reading for both. The pure
  `send_allowed` stays the only decision. A lease that outlives the attempt means a stale
  and a fresh worker never have attempts on the wire together.
- Cons: The send gets less than a full `client_timeout` when the transaction is slow. The
  client's `send()` gains a deadline parameter.
- **Residual, recorded in `KNOWN_TRADEOFFS.md`:** asyncio enforces deadlines at `await`
  points, so a process frozen (GC, VM pause) between the deadline check and a socket write
  can write late. Every client-side check has this; the margins in ADR-004 (quiet period)
  and ADR-012 (retention > window + recovery delay) cover it.
- Cost/latency/complexity profile: The same one transaction ADR-012 already required.

#### Option B: As written in ADR-012/013 (check, then update, then a fresh timeout) — rejected
- How it works: Read `now()`, check, a separate fenced `UPDATE`, then `send()` with its own
  full `client_timeout`.
- Pros: Already described; no client change.
- Cons: The gap in the Context: the attempt can end up to *(update + client_timeout)* after
  the check, and after `last_attempt_at + client_timeout`.
- Why we didn't use it here: It's a bound that doesn't bound.
- When it WOULD be the better choice: Never for a key-lifetime bound; fine for retries with
  no deadline semantics.

#### Option C: The age check in the `UPDATE`'s `WHERE` — rejected
- How it works: `UPDATE … WHERE … AND now() − first_send_started_at + :timeout <= least(…)`.
- Pros: Atomic in one statement.
- Cons: Duplicates the core's `send_allowed` in SQL (two implementations of the safety
  bound), and a 0-row result no longer says *why* (lost claim, or too old?).
- Why we didn't use it here: Option A is just as atomic and keeps one implementation.
- When it WOULD be the better choice: Bulk updates where a round trip per row matters.

#### Option D: Keep B and add a fixed margin to the age check — rejected
- How it works: `elapsed + client_timeout + margin ≤ window`.
- Pros: One config value.
- Cons: A tuned constant, not a bound: a slow enough commit exceeds any margin.
- Why we didn't use it here: Option A makes the bound structural.
- When it WOULD be the better choice: Alongside A for the freeze residual, if it's ever
  measured.

### Part 4 — When per-send config reads keep failing

#### Option A: Bound it by the run's own snapshot, close the gate, then release the claim — CHOSEN
- How it works, on a `FAILED` read (`AUTH_FAILED` is ADR-013's: release and page):
  - **The read closes the gate.** Every per-send read feeds the same gate as the interval
    refresh: a `FAILED` read marks the cached config stale, so this worker claims nothing
    until a later read succeeds (`AUTH_FAILED` and a window below the worst case already
    close it, ADR-012/013). A failed read means refund-api isn't answering, not that this run
    is broken. Without this, a released run is reclaimed on the next poll (S2 puts
    `EXECUTING` first), fails again and is released again, in a loop that starves every other
    run and alerts each cycle.
  - **`APPROVED` run in hand:** release the claim at once. Nothing was sent and nothing ages;
    the gate decides when anyone claims again.
  - **`EXECUTING` run in hand:** a fenced read of `now() − first_send_started_at`. If
    `send_allowed(elapsed, client_timeout, snapshot, snapshot)` fails, the run moves to
    `NEEDS_RECONCILIATION` without sending: any window read later is ≤ the snapshot after
    `min()`, so no read could allow a send. Otherwise back off (full jitter, counted by
    consecutive failed reads) and read again; after `max_failed_reads` in a row (config,
    default = `attempts`), release the claim.
  - Failed reads are counted in memory per claim, never as attempts (ADR-013).
- Pros: Terminates in every case; never sends without a fresh read; never sends a sendable
  run to reconciliation early; a broken refund-api can't make a worker spin on one run.
- Cons: One more counter and one more config value. Another worker whose cache is still
  fresh can claim the released run once, then closes its own gate the same way: at most one
  cycle per worker per refresh interval.
- Cost/latency/complexity profile: One small fenced read per failed config read.

#### Option B: Keep retrying while the lease is held — rejected
- How it works: Back off and re-read until a read succeeds.
- Pros: Simplest.
- Cons: An `APPROVED` run is held for the whole outage; an `EXECUTING` run holds a worker
  until the outage ends. The worker is "alive", so liveness never fires.
- Why we didn't use it here: A loop with no exit.
- When it WOULD be the better choice: Never with a lease that only measures liveness.

#### Option C: Count failed reads as attempts — rejected
- How it works: A failed read uses one of the run's attempts.
- Pros: Reuses the existing limit.
- Cons: Contradicts ADR-013 (nothing reached refund-api); a 30-second blip uses up the
  attempts and sends sendable runs to reconciliation.
- Why we didn't use it here: Locked ADR, and the wrong failure mode.
- When it WOULD be the better choice: If the read and the send were one request.

#### Option D: Move `EXECUTING` runs to `NEEDS_RECONCILIATION` after N failed reads — rejected
- How it works: Give up on the run after N failed reads.
- Pros: Bounded.
- Cons: Reconciliation never re-sends (ADR-008). If the first attempt never reached
  refund-api, the lookup finds nothing and the refund goes to a human, for a blip the
  run's own window could have ridden out.
- Why we didn't use it here: It turns a short outage into manual work.
- When it WOULD be the better choice: A provider whose config can't be read separately
  from sending.

### Part 5 — How a test process gets the fault hooks

#### Option A: A test entry point in `agent_testing` (ADR-011's pattern) — CHOSEN
- How it works: Production `run_worker(settings, hooks=NO_FAULTS)`. Seams (ADR-007):
  `before_refund_call`, `after_refund_response` (ADR-004 crash points) and
  `after_capacity_read` (ADR-005 race pause). `python -m agent_testing.worker` reads
  `CRASH_POINT`/`DEBUG_FAULT_INJECTION`, builds the hooks and calls `run_worker`; it refuses
  to start when `APP_ENV=production`. The production entry point refuses to start if either
  variable is set, since it can't honor them: it means the wrong entry point was deployed.
  In-process tests pass hooks straight to `run_worker`.
- Pros: Production code has no path to test code and needs no import-linter exception.
  The production image doesn't contain `agent_testing` (ADR-007 image test).
- Cons: A second entry point to keep in step (it's a few lines: env → hooks → `run_worker`).
- Cost/latency/complexity profile: One small module.

#### Option B: ADR-007's gated factory with a lazy import — rejected
- How it works: `agent.worker` checks `DEBUG_FAULT_INJECTION` and imports `agent_testing`
  inside that branch.
- Pros: One entry point; ADR-007's wording.
- Cons: Production source holds a path to test code behind a flag and an import-linter
  exception (ADR-011 rejected this for refund-api for the same reason).
- Why we didn't use it here: The worker is its own process with tiny wiring, which is
  ADR-011's case for Option A, not its exception.
- When it WOULD be the better choice: The chat API's scripted model (ADR-006), selected by
  config inside a process whose wiring is large.

#### Option C: In-process tests only, no crash process — rejected
- How it works: Tests call the worker directly with hooks that raise.
- Pros: Fast, no subprocess.
- Cons: A raised exception unwinds `finally` blocks and closes connections cleanly. `os._exit`
  doesn't, which is the point of ADR-004's crash tests.
- Why we didn't use it here: It tests a crash that's too polite.
- When it WOULD be the better choice: As the fast tier next to Option A (and it is: most
  worker tests run in-process).

### Part 6 — Alerts and pages

#### Option A: An `Alerter` protocol (alert, page), a structured-log implementation, a recording fake for tests — CHOSEN
- How it works: `alert(kind, **fields)` and `page(kind, **fields)`. Production logs one JSON
  line at `WARNING`/`CRITICAL` with `alert`/`page` set. Tests assert on a recorder. Project 5
  (observability) or the deploy ADR plugs in a real channel.
  - **Rate-limited by `kind`** (plus the `run_id` for per-run pages such as S12's, so one
    run's page can't hide another's): at most one line per key per interval (config). Repeats
    inside the interval are counted, not dropped silently: the next line for that `kind`
    carries `suppressed=<n>`. A long outage otherwise alerts from every worker on every
    failed read.
- Pros: "401 pages" and "N lock timeouts alert" become assertable. No secrets or network in
  CI. A real channel later is one implementation.
- Cons: Nothing actually wakes anyone yet; recorded in `KNOWN_TRADEOFFS.md`.
- Cost/latency/complexity profile: One small protocol.

#### Option B: An `alerts` table — rejected
- How it works: Rows written in the same transaction as the cause.
- Pros: Durable, queryable, transactional with the event.
- Cons: Needs a dispatcher to do anything, plus schema, grants and retention.
- Why we didn't use it here: Storage without delivery; Project 5 owns delivery.
- When it WOULD be the better choice: When alerts must survive a crash between cause and
  delivery (then the dispatcher reads this table).

#### Option C: A real channel now (Slack webhook, PagerDuty) — rejected
- How it works: HTTP calls from the worker.
- Pros: Real paging.
- Cons: Secrets, network in tests, and a failure mode inside the send loop.
- Why we didn't use it here: Too early; nothing is deployed.
- When it WOULD be the better choice: At deployment, behind Option A's protocol.

#### Option D: Plain log lines, no seam — rejected
- How it works: `logger.error(...)` where an alert is due.
- Pros: Nothing to build.
- Cons: Tests would assert on log text; renaming a message breaks them, and a missing alert
  looks the same as a quiet one.
- Why we didn't use it here: An alert nobody can assert isn't tested.
- When it WOULD be the better choice: Scripts and one-off tools.

### Smaller decisions (one viable choice each, alternatives noted)
- **S1. Window snapshot column:** `runs.send_window_ms BIGINT`, exact, because `GET /config`
  reports integer milliseconds (ADR-013). Alternatives: `send_window_seconds INTEGER`
  (ADR-012's name; floor would be the safe rounding, but units then differ from the read);
  `INTERVAL` (exact, but `send_attempts`/ms integers keep one style). New `CHECK`s:
  `send_attempts >= 0`; `(send_window_ms IS NULL) = (first_send_started_at IS NULL)`;
  `send_window_ms > 0`; `first_send_started_at IS NULL ⇒ send_attempts = 0`. Kept out of the
  ADR-008 parity mirror (they aren't lifecycle invariants) and tested on their own. The
  migration refuses if a sent run has no snapshot (as 0004 does on refund-api).
- **S2. Claim order:** `EXECUTING` first (they're aging against their window, and are
  usually a crashed worker's run), then oldest `approved_at`.
- **S3. One run per worker process at a time.** Scale with processes. Alternative: N
  concurrent slots per process, if throughput ever needs it.
- **S4. Window read below the worst case while holding an `APPROVED` run:** release,
  nothing sent. Starting a first send is a new commitment, same as a new claim. An
  `EXECUTING` run in hand goes through the age check with the smaller value (ADR-013).
- **S5. `FAIL_CLOSED` at pre-send:** ADR-009's separate fenced transaction increments
  `presend_attempts`; the worker keeps its claim and backs off (as for `55P03`) until the
  maximum moves the run to `ESCALATED`.
- **S6. Unexpected exceptions:** alert, cancel the run's tasks, best-effort fenced release
  with the S12 cooldown; the run stays in its status; never classified as an outcome. A database error while
  recording a 201 is the same case: the next round replays the same key (ADR-004
  `after_refund_response`).
- **S7. Shutdown (SIGTERM):** stop at the next step boundary; an attempt on the wire finishes
  inside its budget and its outcome is recorded; then release the claim. Bounded by a grace
  period > `client_timeout`; past it, exit and let the lease expire.
- **S8. Database time limits:** the heartbeat has its own connection; every worker
  transaction sets `statement_timeout` and `lock_timeout`, so no database call outlives the
  liveness bound. A heartbeat **error** (not 0 rows) isn't a lost claim, but Part 3's send
  transaction requires a live lease, so a worker that can't renew can't send.
- **S9. Releasing a claim:** cancel the heartbeat task first, then `worker_id = NULL,
  lease_expires_at = NULL, claim_version = claim_version + 1 WHERE id = :id AND
  claim_version = :v`. **Every release bumps the version**, so the releasing worker's version
  is dead the moment it lets go: without the bump it stays valid until someone else claims,
  and ADR-005's `APPROVED → EXECUTING` move, fenced by `claim_version` alone, could still
  commit from a stray task. The move to `NEEDS_RECONCILIATION` also clears the claim, so the
  reconciler doesn't wait for a lease.
- **S10. Customer replies after `APPROVED`:** the worker appends `events(kind='reply',
  payload={'template': <id>, ...})` in the outcome transaction. Rendering the text is the
  chat API's (ADR-001/008 templates).
- **S11. Lease sizing at startup:** lease ≥ 3 × heartbeat (ADR-004), and the liveness bound
  `client_timeout + backoff_cap + lock_timeout + margin` (ADR-004) applies to each step: a
  config read, a transaction, a send or a backoff sleep.
- **S12. Releases that expect the failure to recur wait out a cooldown.** The gate (Part 4)
  stops the config-read loop, but two other releases would loop the same way: ADR-005's
  release after N consecutive `55P03` timeouts (the run is still the oldest `APPROVED`, so it
  is reclaimed first and waits on the same customer lock), and S6's release after an
  unexpected exception that recurs for that run.
  - **Cooldown column:** these releases are S9 releases that also set `claimable_after =
    now() + claim_cooldown`. The claim query adds `AND (claimable_after IS NULL OR
    claimable_after <= now())` in both the subselect and the outer `WHERE`, and a claim
    clears it. The lease keeps its one meaning ("a live worker owns this"), and `worker_id`
    stays ownership, as ADR-004 and ADR-008 define them, for anything that reads it (admin
    bulk-escalate, debugging, alerts). Releases with nothing to wait for (S7 shutdown, a
    `HOLD`, which the claim filter already excludes) leave it `NULL`.
  - **A run that fails every time:** a cooldown spaces retries out but never stops them, and
    an `EXECUTING` run whose exception comes before the age check never ages into
    reconciliation. So `runs.release_streak` counts consecutive cooldown releases: incremented
    in the release transaction, **reset to 0 in the same transaction as any progress** for the
    run (the pre-send transaction, whatever its outcome; a Part 3 transaction that records an
    attempt; an outcome transaction). Part 4's failed-read releases don't count: they close
    the gate and aren't the run's fault. At `release_streak_limit` (config):
    - **`APPROVED` → `ESCALATED`** (`escalated_at`, `escalated` event with the "a person will
      follow up" template, claim cleared, alert). Safe: nothing was sent. An operational
      escalation, so an event and an alert, not a decision row (ADR-009: worker outcomes
      aren't decisions).
    - **`EXECUTING`: status unchanged**, because its outcome is unknown and only the age check
      or the reconciler may move it. Page with the `run_id` so a person looks at it; it keeps
      its cooldown between claims.
  - Alternatives: borrowing the lease for the cooldown (no column, but the lease gets two
    meanings and needs extra rules: the cooldown release must bump the version and cancel the
    heartbeat before committing); an in-memory skip list (doesn't stop other workers, lost on
    restart); no streak (a run that always fails cycles forever, once per cooldown).
- **S13. Claim index:** a partial index on `runs (approved_at) WHERE status IN ('APPROVED',
  'EXECUTING')`, so the claim subselect scans only claimable runs however many finished ones
  pile up; the `EXECUTING`-first sort runs over that small set. (Indexing `status` itself
  would lean on the enum's declaration order for the sort, which is fragile.)

## Decision
Part 1 A, Part 2 A, Part 3 A, Part 4 A, Part 5 A, Part 6 A, and S1–S13.

### One claimed run, start to finish
1. **Claim** (Part 2), only while the cached config is fresh, authorized and its window ≥
   the worst case (ADR-012 Part 4). Start the heartbeat task.
2. **`APPROVED`:** read config → (failed: release, Part 4; 401: release, page; window <
   worst case: release, S4) → customer-locked transaction: `evaluate_presend` once →
   `PROCEED`: `EXECUTING`, `first_send_started_at = last_attempt_at = now()`,
   `refund_outcome = unknown`, `send_window_ms` from this read; `OUT_OF_POLICY`:
   `ESCALATED` + event; `HOLD`: release; `FAIL_CLOSED`: S5; `55P03`: ADR-005, then S12.
   Decision row in the same transaction. That transaction commits before the first send's
   budget starts.
3. **`EXECUTING`, each attempt, the first included:** read config (the first attempt reuses
   step 2's read) → start the deadline → Part 3's transaction (age check + attempt limit +
   record, or `NEEDS_RECONCILIATION`) → send within the budget → classify (ADR-012) → `CONFIRMED`: `COMPLETED`; `REJECTED`: `ESCALATED`;
   `RECONCILE`: `NEEDS_RECONCILIATION`; `RETRY`: back off by `send_attempts` (persisted)
   and loop. Every write fenced by `claim_version`.
4. **Lost claim** at any point (0-row heartbeat or fenced write): cancel, send nothing more,
   write nothing more.

### Tests this ADR adds (each with a negative control)
Integration tests run against real Postgres with a scripted fake `RefundClient` in
`agent_testing`; every wait is bounded. The `os._exit` crash tests against a real refund-api
are the next step and use Part 5's entry point.

| Test | Negative control |
|---|---|
| Two workers race for one run → exactly one claim; `COMPLETED`/`ESCALATED`/`NEEDS_RECONCILIATION` never claimed | Claim without the status condition claims a finished run |
| Breaker holds only policy-approved `APPROVED`; kill switch holds all `APPROVED`; `EXECUTING` claimed under either | Breaker scope used for the kill switch claims a reviewer-approved run |
| Gate: config missing, stale, 401, or window < worst case → no claims; 401 pages | Worker without the gate claims and sends |
| Policy check runs exactly once: a resumed `EXECUTING` run never calls `evaluate_presend` (tight-limit case is recorded, not escalated) | Re-check on resume escalates it |
| Pre-send outcomes: `OUT_OF_POLICY` → `ESCALATED` + event + decision, 0 sends; `HOLD` → released, `APPROVED`; `FAIL_CLOSED` → `presend_attempts + 1`, escalates at max | Each path asserted against a planted wrong transition |
| Lock: `55P03` → stays `APPROVED`, claim kept; N in a row → alert, released, 0 sends; another DB error isn't retried as a lock wait | Treating every DB error as `55P03` retries a constraint violation |
| Heartbeat 0 rows mid-backoff → no further send, no write | A worker ignoring the heartbeat result sends again |
| Step overruns its bound → renewal stops → another worker claims after expiry | A heartbeat that renews unconditionally keeps the claim forever (bounded wait fails) |
| Stale worker: its Part 3 transaction after another claim → 0 rows, no send | Fencing without `claim_version` sends |
| **Part 3:** a slow attempt transaction (seam delay) shortens the send's budget; the attempt ends by `last_attempt_at + client_timeout` | A fresh `client_timeout` at `send()` overruns it |
| **Part 3:** while an attempt is on the wire, another worker can't claim the run | Without the lease extension, it can |
| Per-send read fails → no POST, `send_attempts` unchanged; invalid read → same plus alert; 401 → no POST, released, page | Counting the failed read reaches the limit early |
| First send sets `send_window_ms` from the read just before it; a decrease before a retry → `NEEDS_RECONCILIATION`, no POST | A snapshot from the cache sends the retry |
| Window raised after the first send, run resumed past the old window → no send | Age check against the current read only sends it |
| Attempt count survives a worker restart | An in-memory count sends again |
| **Part 4:** `EXECUTING` past its snapshot with reads failing → `NEEDS_RECONCILIATION`, no POST; N failed reads → released; `APPROVED` + failed read → released | Option B's loop never exits (bounded wait fails) |
| **Part 4 gate:** per-send reads fail while the cached config is fresh → the released run isn't reclaimed until a read succeeds | Without marking the cache stale, it's reclaimed in a loop (claim count over a bounded window) |
| **First send's budget:** a slow `APPROVED → EXECUTING` transaction (lock-wait seam) doesn't shorten the first send's budget, and the first send's attempt is recorded by Part 3's transaction (`send_attempts = 1`, lease extended) | Starting the deadline before the policy transaction cuts the first send short; skipping Part 3 for it leaves `send_attempts = 0` after a send |
| **S12 cooldown:** N `55P03` timeouts → released with `claimable_after`; the run isn't reclaimed before it, another run is claimed meanwhile, and it is reclaimed after | A release without `claimable_after` is reclaimed on the next poll |
| **S12 streak:** an `APPROVED` run that fails on every claim → `ESCALATED` at the limit, 0 sends; an `EXECUTING` one → a page with its `run_id`, status and outcome unchanged; any progress resets the streak | Without the streak, the `APPROVED` run is reclaimed forever (bounded wait fails); a streak that moves `EXECUTING` runs breaks `terminal_not_unknown` and the test |
| **S9:** after any release, a fenced write with the releaser's old `claim_version` (the pre-send move included) affects 0 rows | A release without the version bump lets the stray pre-send commit |
| **Alerts:** repeated alerts of one `kind` inside the interval → one line, then `suppressed=<n>` on the next | An unlimited alerter emits one line per call |
| Classification → transition table: each `SendKind` lands in ADR-008's status, outcome and events | A planted `RECONCILE → ESCALATED` breaks the `terminal_not_unknown` check and the test |
| **Part 5:** production entry point refuses to start with `CRASH_POINT` set; test entry point refuses `APP_ENV=production` | Either check removed → starts |
| **S1:** the new `CHECK`s reject each bad combination; the migration refuses a sent run without a snapshot | Each insert allowed by the owner role with the `CHECK` dropped |

## Consequences
- `runs` gains `send_attempts`, `send_window_ms`, `claimable_after`, `release_streak`
  (`>= 0`), and the S13 claim index (agent migration 0002).
- `RefundClient.send()` and `read_config()` take a deadline; the client's own total timeout
  stays as the inner limit.
- New modules: `agent.worker` (loop, heartbeat, attempt), worker queries under `agent.db`,
  `agent.alerts`; `agent_testing` gains the fake client, the fault hooks and the test entry
  point.
- New config: poll interval, lease, heartbeat interval, liveness margin, `max_failed_reads`,
  config refresh interval and staleness, shutdown grace, `lock_timeout`/`statement_timeout`,
  pre-send and lock-retry maximums, `claim_cooldown`, `release_streak_limit`, alert rate-limit
  interval. All
  validated at startup.
- `KNOWN_TRADEOFFS.md` gains: the process-freeze residual (Part 3); no real alert channel yet
  (Part 6), so "401 pages" is a `CRITICAL` log line for now. That's less urgent than when
  ADR-012 made it a page, because the send gate means a wrong token now **stops sends**
  instead of escalating every refund; one run per process (S3).
- Pointer lines go into ADR-004 (step 7: claim statement, attempt budget), ADR-005 (release
  after N lock timeouts waits out a cooldown, S12), ADR-007 (worker test entry point),
  ADR-012 (Part 3 order, `send_window_ms`) and ADR-013 (Part 4 corrects "the run keeps aging,
  so a long failure ends in the age check"; failed per-send reads close the gate) when this
  ADR is locked.
- **Revisit if:** throughput needs several runs per process (S3); refunds need sub-second
  pickup (Part 2 C); the deploy host makes a separate process costly (Part 1 B as a deploy
  option).

## Interview-ready summary
"The worker is its own process polling the runs table. The run row already is the queue, so
I didn't add Celery or a job library that would become a second source of truth fighting my
lease and fencing. One `SKIP LOCKED` statement claims a run without workers piling onto the
same row, and it keeps the original compare-and-set conditions so the guarantee doesn't
depend on the lock. Planning it, I found two holes in my own locked design. First, my age
check, the attempt record and the send were separate steps, and the send started a fresh
timeout, so an attempt could end after the bound the check promised. Now the check and the
record are one transaction with one clock reading, and the send only gets what's left of a
budget that started before that transaction. Second, if the provider's config read kept
failing, the worker never reached the age check and could hold a run forever. Now the run's
own window snapshot bounds it, because no later read could ever allow a send past it."

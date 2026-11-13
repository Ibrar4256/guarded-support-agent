# ADR-017: What Shows a Failed Lookup Is the Run's — a Successful Lookup, Not a Config Read

**Status:** Accepted (2026-11-12). Option chosen by the user while building ADR-016's reconcile
loop. Revised 2026-11-13 after two outside reviews: the stale-unknown signal gains a page (S3),
except for runs whose latest lookup was a "not found" the kill switch held (S4); the
degraded-provider residual is recorded; the send path's blindness to the same outage is left to
ADR-018.

**Locked 2026-11-13.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Precedence:** supersedes one clause of [ADR-016](016-reconciler.md) Part 3: what counts as
evidence that a repeated refund-api-type lookup failure is the run's own. Everything else in
ADR-016 stands: every reconciler release applies the cooldown, a refund-api-type failure marks
the gate stale and alerts (401/403 page), a run's own failure counts toward the streak and pages
at the limit.

## Context
ADR-016 Part 3 counts a refund-api-type lookup failure (no response, a non-200, a body that isn't
strict JSON, 401/403) toward the run's streak "only when it repeats for the same run with a
successful config read in between: refund-api answered the read, so the failure is evidently this
run's". That assumes a working `GET /config` means refund-api's lookups work too.

It doesn't. `GET /config` returns a response built once at startup from settings
(`services/refund_api/src/refund_api/app.py`, `get_config`); `GET /refunds` queries refund-api's
database (`service.py`, `list_refunds`). **With refund-api's database down, every config read
succeeds and every lookup fails.** Under ADR-016's rule:
- every uncertain run's second failure counts, every run reaches the streak limit and every run
  pages (rate-limited per run, so N pages per interval): the outcome ADR-016 rejected as its
  Option B ("a refund-api outage pushes every uncertain run to the limit, and every run pages");
- the gate hardly closes: each pass's fresh read (ADR-016 Part 2) reopens it.

Constraints: the agent can't see refund-api's internals (ADR-007: no shared code); one process,
one reconcile slot; a broken run must still reach a person.

## Options Considered

### Option A: Evidence is another run's lookup answered since this run's last failure — CHOSEN
- How it works: the reconcile loop keeps, in process memory, a count of lookups refund-api
  **answered** (a 200 with a well-formed list: found, not found, or a mismatched row) and, per
  run, that count at the run's last refund-api-type failure. A repeat counts toward the streak only
  if the count has moved: refund-api's lookup path answered someone else in between, so this run's
  failure is evidently its own. A run's mark is cleared when its own lookup is answered.
- Pros: tests the same path that failed (the lookup), not a proxy; a database outage produces no
  streak and no per-run pages; ADR-016's one-bad-`action_id` case still pages, because the other
  runs' lookups are answered; agent-only, no contract change.
- Cons: **a run that fails alone** (no other uncertain run is answered in between) never reaches
  the streak limit, so it never pages through the streak. The stale-unknown signal (ADR-004,
  ADR-016) is then its route to a person, so that signal must page, not only alert (S3). A
  **degraded** refund-api (some lookups failing, others answered) moves the counter, so its
  failing runs build streaks and page per run although the cause is one dependency; the streak
  limit and per-run rate limiting keep that bounded. A restart forgets the marks (one extra
  cycle, as in ADR-016).
- Cost/latency/complexity profile: an integer and a dict per process; no extra requests.

### Option B: Make `GET /config` probe refund-api's database — rejected
- How it works: `/config` runs a query, so a database outage fails config reads and closes the
  gate, and ADR-016's rule works as written.
- Pros: the gate closes for both loops during a database outage.
- Cons: a change to the other service and its contract; every send's config read (ADR-013) now
  depends on refund-api's database latency; it probes a different query than the lookup, so a
  failure only the lookup path hits (a bad index, one slow plan, a lock on `refunds`) still looks
  run-specific.
- Why we didn't use it here: it proves "the database answers `SELECT 1`", not "lookups work".
- When it WOULD be the better choice: a provider with a real readiness endpoint that covers its
  lookup path, documented as such.

### Option C: Keep ADR-016's rule (a successful config read in between) — rejected
- How it works: as locked.
- Pros: no change; a run that fails alone still pages.
- Cons: the database outage above pages once per uncertain run per interval.
- Why we didn't use it here: it misreads a static endpoint as evidence about the lookup path.
- When it WOULD be the better choice: a provider whose config endpoint shares the lookup's
  dependencies.

### Option D: Detect an outage by count: K distinct runs failing inside a window — rejected
- How it works: if K or more runs fail refund-api-type inside T seconds, treat it as an outage and
  count nothing; otherwise count repeats.
- Pros: also covers a run that fails alone (below K it counts).
- Cons: two thresholds to tune; with fewer than K uncertain runs an outage is counted per run
  again; more state, and the result depends on how many runs happen to be uncertain.
- Why we didn't use it here: Option A asks the direct question ("does the lookup path answer
  anyone?") with no thresholds.
- When it WOULD be the better choice: many reconcile slots, where answered lookups for other runs
  are frequent enough that A's evidence is never missing.

### Smaller decisions
- **S1. One signal per outage:** the refund-api-type lookup alert is rate-limited as one key
  (the run goes in a field, not in the alert's `run_id`), so an outage is one line per interval,
  not one per run. A 401/403 pages under the same global key the gate uses. A run's own failure
  and the streak-limit page keep the `run_id` key (ADR-014 Part 6).
- **S2. The core tells the two apart:** `classify_lookup` keeps its three kinds and gains a fault
  (`provider` or `run`) on `lookup_failed`, so the split is pure and unit-tested, not re-derived
  from status codes in the worker.
- **S3. The stale-unknown signal pages:** a second threshold, `RECONCILER_STALE_PAGE_SECONDS`
  (greater than `RECONCILER_STALE_ALERT_SECONDS`, refused at startup otherwise): a
  `NEEDS_RECONCILIATION` run unresolved past it pages, rate-limited per run. ADR-004's alert
  stays as the earlier, softer signal. Without the page, "a broken run still reaches a person"
  would depend on someone reading alerts.
- **S4. Not for runs the kill switch actually held:** the switch holds only "not found"
  decisions (ADR-016 Part 4), and those runs pile up by design. So the per-run stale page is
  skipped **only for a run whose most recent lookup was a "not found" held by the switch** (the
  per-run alert still fires), and one page with no `run_id` says the switch has held runs past
  the page threshold. Every other uncertain run keeps its per-run page while the switch is on: a
  run stuck for its own reason (its lookups failing, a mismatched row) isn't held by the switch,
  and suppressing it would cut its only route to a person for the whole incident. Otherwise a
  long incident would page once per held run per interval. The "held" mark lives in the
  reconcile loop's memory, beside ADR-017's evidence, and is cleared by any other outcome for the
  run; a restart forgets it, so a held run pages per run until its next pass: the safe
  direction.

## Decision
Option A, S1–S4.

### Tests (each with a negative control)
| Test | Negative control |
|---|---|
| refund-api's lookups fail for every run while config reads succeed (ten runs, each failing at least twice): no streak increments, no page | ADR-016's rule (a config read in between) increments streaks |
| One `action_id`'s lookup fails while the others are answered: the others reconcile, the failing run reaches the limit and pages | Without the cooldown it is reclaimed in a loop (ADR-016's control) |
| A run failing alone, repeatedly: no streak; the stale-unknown alert fires past its threshold | No alert below the threshold |
| `classify_lookup`: non-200, malformed body → `provider`; a mismatched row → `run` | Swapping the two fails the table |
| A lone failing run pages once past `RECONCILER_STALE_PAGE_SECONDS`, once per run per interval | Alert-only never pages |
| Kill switch on, one held run and one run failing on its own, both past the page threshold: the held run gets no per-run page (only the global one, and its alert), the failing run gets its per-run page | Suppressing every run while the switch is on silences the failing one; paging held runs pages once per held run |
| A page threshold not above the alert threshold is refused at startup | — |

## Consequences
- The reconcile loop holds a small in-memory evidence record.
- `KNOWN_TRADEOFFS.md`: a run that fails alone relies on the stale-unknown page, so
  `RECONCILER_STALE_PAGE_SECONDS` is its paging delay; a degraded (not down) refund-api pages per
  failing run, bounded by the streak limit and the rate limit.
- A new setting, `RECONCILER_STALE_PAGE_SECONDS`.
- **Not solved here: the send path is blind to the same outage.** The claim gate is fed only by
  config reads, so with refund-api's database down it stays open; sends get fast 5xx, attempts
  run out in seconds, runs go to reconciliation, and after recovery each is correctly found
  missing and ends `rejected`/`ESCALATED`: every refund approved during the outage is escalated
  to a person. No money is lost. ADR-018 (a provider circuit breaker driven by real request
  outcomes on sends and lookups) is to address it before the worker is deployed.
- Pointer line in ADR-016 Part 3 once this is locked.
- **Revisit if:** refund-api gains a readiness endpoint covering lookups (Option B); the
  reconciler gets several slots (Option D becomes cheap); **ADR-018's circuit breaker may absorb
  this evidence counter** (one mechanism judging provider health from real outcomes for both
  loops).

## Interview-ready summary
"My reconciler had to tell 'refund-api is down' from 'this one refund is broken', because only
the second should page per run. The first design used a successful config read as the evidence,
until I checked refund-api's code: its config endpoint is static and never touches the database,
so a database outage would have looked like every run being broken at once. I changed the
evidence to the thing that actually failed: a failure counts against the run only if refund-api
answered somebody else's lookup in between. The cost is that a run failing alone never pages
through its streak, so it reaches a person through a stale-unknown page instead, and I wrote that
trade-off down."

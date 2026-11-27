# ADR-030: The Eval Replay Gate in CI — Replayed Results Pinned to a Committed Report

**Status:** Proposed (2026-11-27). Revised the same day after one outside review: the derived
totals are compared too (a summing bug leaves every case-run equal); the Postgres image is pinned
and the first CI run against locally made pins is the acceptance check; a re-pin prints which
case-runs moved; the pin is validated both ways; both exit codes and k's single source are
stated; re-pinning without a live call is limited to recordings that still hit. Not built.

**Locked 2026-11-27.** No further revisions; issues found from here on are fixed in code
and tests, or recorded in a new ADR that supersedes this one.

**Extends:** ADR-026 S7 ("a per-PR job runs every suite in `replay` (no keys)"), which says what
runs but not what fails it. Consistent with ADR-025 S7: per-PR replay is a regression gate on
our code, not a fresh measurement of the models; reported numbers still come from recording
runs. Pointer lines go into ADR-026 and ADR-025 when this is locked.

## Context

Three suites are committed with their recordings: smoke (2 cases, k=1), adversarial (25 cases,
k=3) and resolution (30 cases, k=3), 447 recordings in all. CI doesn't replay them, so nothing
stops a change from breaking them, and on 2026-11-27 that already happened: the smoke suite,
recorded before the warrant step existed, couldn't replay (one warrant call was never recorded),
and nobody would have noticed until someone ran it by hand.

What the replay gate must catch, in order of how much it matters:

1. **A change that lets an attack through again while every recording still replays.** The
   recorded model answers are fixed; the code that judges them (policy rules, C02, the warrant
   checks, the judges, the runner) is what changes in a PR. Loosen W01 and the adversarial suite
   replays without a single error, and an attack that ended in review now ends in a refund.
   Replay's exit status today counts only recording errors, so CI would stay green.
2. **A changed prompt, model or key**, which makes a recording miss. Replay already fails on it.
3. **A quiet change in how results are counted.** S4's split counted a seeded past ticket's
   approved run as the case's (fixed in `e5120b0`): a wrong number with no error anywhere.

Constraints: zero spend and no secrets in CI (ADR-025, ADR-026); one developer; the gate must
be fast enough to run on every push. Measured: replaying all three suites takes about one
minute (smoke 3 s, adversarial 27 s, resolution 32 s locally). **Replay is deterministic:** two
zero-call record-missing runs over the adversarial and resolution suites gave identical results
for all 165 case-runs, including every judge's detail string, the split and the judge totals.
That was one machine. Two things could still differ in CI: the judges' detail strings hold only
amounts, the case's own order ids, statuses, sorted lists and message numbers (no
database-generated id, sequence value or float), and every query a judge or the split reads is
ordered; but the Postgres image is a floating tag (`postgres:15-alpine`, 15.18 locally,
`en_US.utf8`), so CI can pull another minor version than the one the pins were made on.

## Options Considered

### Option A: Fail on recording errors, and on any difference from a pinned report, case-run by case-run — CHOSEN
- How it works: a committed file, `services/agent/evals/pinned.json`, names one report per
  suite. The CI job replays every suite it names and compares, for every case and run index:
  whether it passed, its split bucket, and each verdict's judge, pass and detail. Any difference,
  a case-run that's new or gone, or a recording error fails the job and prints each changed
  case-run as old → new. The report's derived totals (per category, per judge, the split, the
  reviews by cause) are compared too: a bug in the code that sums them leaves every case-run
  equal and the headline numbers wrong. Tokens, latency, commit, date, the recording run id and
  the key counts aren't compared: they describe the run, not the result.
- Pros: catches all three failure kinds above, including the one that matters most; a change
  that legitimately moves an outcome is visible in review, because the PR must update the pinned
  report and the diff shows which cases moved; it pins today's known failures too, so a fix is
  noticed as clearly as a regression.
- Cons: strict both ways: an improvement also turns the job red until the pin is updated; every
  new case needs a recording run and a new pin; a careless author can re-pin to make the job
  green without reading the diff.
- Cost/latency/complexity profile: no spend; about one minute of CI; one comparison function,
  one pointer file, one script; re-pinning costs a zero-call rerun (about a minute per suite,
  locally, with the database).

### Option B: Fail on recording errors only — rejected
- How it works: the CI job runs `replay` for each suite and fails on its exit status, which
  counts missing recordings, another anchor, a real date in a prompt and invariant violations.
- Pros: the literal reading of ADR-026 S7; nothing to maintain beyond the job; outcome changes
  never block a PR.
- Cons: blind to the most important regression: a policy, warrant-check or judge change that
  lets an attack through again keeps every recording valid, so the job stays green; it would
  also have missed the S4 split bug.
- Why we didn't use it here: the project's safety claim is about what the code lets through, and
  this gate doesn't look at that at all.
- When it WOULD be the better choice: when replay isn't deterministic (a judge with a wall-clock
  date, an LLM judge sampled at temperature above zero), or very early, when outcomes change in
  most PRs and re-pinning would be churn without information.

### Option C: Pin only the safety counts, one-sided (forbidden approvals and false reviews may not rise) — rejected
- How it works: the job computes a few aggregate numbers per suite and fails only when one gets
  worse than the pinned value: forbidden approvals in the adversarial suite, reviews caused by
  the new layer in the resolution suite, errors anywhere.
- Pros: only real regressions block a PR; improvements merge without a re-pin; small, readable
  thresholds.
- Cons: aggregates hide swaps (one attack gets through while another stops, same count); every
  outcome that isn't one of the chosen counts can move silently (a reply template, an escalation
  reason, a split bucket); the thresholds themselves become a second thing to keep right.
- Why we didn't use it here: replay is exact, so there's no noise to tolerate; giving up exactness
  buys convenience and costs coverage.
- When it WOULD be the better choice: for live or nightly runs, where the model answers differ
  run to run and only a threshold on counts makes sense; or with hundreds of cases, where a
  per-case diff stops being read.

### Option D: A nightly job against a live model instead of a per-PR replay gate — rejected
- How it works: ADR-025 S7's drift check: a scheduled job with provider keys runs a shard of the
  cases live and reports the results.
- Pros: measures the model as it is today, catches provider and model drift, finds new failures
  that the recordings can't.
- Cons: needs secrets in CI, spends free-tier quota that the demo and recording runs need, isn't
  deterministic, and runs after the merge rather than on the PR, so it can't stop a regression
  from landing.
- Why we didn't use it here: it answers a different question (has the model changed?) than the
  gate (has our code changed what happens?). It stays a later, separate step, as ADR-025 and
  ADR-026 already say.
- When it WOULD be the better choice: alongside this gate once there's quota to spare, or with a
  paid model and a hard spending cap; never as its replacement.

### Option E: Snapshot the whole report file, byte for byte — rejected
- How it works: the job regenerates each suite's report and compares the file with the committed
  one.
- Pros: nothing to choose about which fields matter; trivially simple.
- Cons: a report holds the commit, the date, the mode, the recording run id and latency, which
  change on every run, so every PR would need a re-pin, and the noise would hide the changes
  that matter.
- Why we didn't use it here: it fails on everything, which teaches people to re-pin without
  looking.
- When it WOULD be the better choice: if reports held only results (no run metadata); then it
  collapses into Option A.

### Option F: Require every case to pass — rejected
- How it works: the job fails if any judge fails in any case-run.
- Pros: the simplest possible rule; a green job means a clean suite.
- Cons: the suites hold honest, known failures (`legal_threat_escalates` fails its escalation
  judge in every run; `status_cancelled` fails one run in three), so the job would be red from
  day one, or those cases would have to be weakened or deleted, which hides exactly what evals
  are for.
- Why we didn't use it here: it punishes recording real failures.
- When it WOULD be the better choice: for a suite that's green by design, such as a set of
  scripted regression cases (ADR-006) where every failure is a bug.

## Decision

Option A, in this order:

- **S1. The pin.** `services/agent/evals/pinned.json` maps each suite to one report file name in
  `services/agent/evals/reports/`. It's valid only if the suites it lists and the suite folders
  under `evals/cases/` are the same set, both ways (a new suite can't skip the gate, and a
  deleted one can't leave a pin that checks nothing), and every named report exists, names that
  suite, was written by a recording run (`record` or `record-missing`, ADR-026 S5), has no
  errors and carries split buckets (reports from S4 on). The pinned report is the only source
  of the suite's k: no case or suite file declares one today, and if one ever does, a mismatch
  is a validation error. The first pins are `2e43a1c004d4` (adversarial), `8f42152483fa`
  (resolution) and `598f4e0e0fd9` (smoke).
- **S2. The comparison.** `replay` takes `--expect <report>`: after the run, it compares every
  case-run's pass, bucket and verdicts (judge, pass, detail) with the report's, by case and run
  index, and lists every difference as `case run N: old → new`, plus case-runs that appear on
  only one side; then the derived totals (categories, judges, split, reviews by cause), each
  difference listed the same way. Exit codes: 4 for differences only; 1 for recording errors,
  with or without differences, and both are printed (an errored case-run has no result to
  compare, so the error comes first).
- **S3. The CI job.** A separate job, `eval replay`, in parallel with the service jobs: uv,
  Postgres with the superuser (the runner clones a database per case, as e2e does), then a script
  that validates the pin and replays each pinned suite with `--expect`. No secrets; a timeout of
  15 minutes; provider keys never set. The Postgres image is pinned to the version the pins were
  made on (`postgres:15.18-alpine` in `docker-compose.yml`, so local, CI and e2e share it; a
  Postgres upgrade becomes its own change, re-pinned if anything moves). When a PR changes
  `pinned.json` or a pinned report, the job also compares the base commit's pinned report with
  the new one (the same comparison, two files, no replay) and prints the case-runs that moved,
  so the reviewer reads a list, not two JSON files. **Acceptance:** the first CI run passes
  against pins made locally; if it doesn't, the difference is found and fixed before the job
  is required.
- **S4. Re-pinning.** A change that moves an outcome while every recording still hits updates
  the pin in the same PR: a record-missing rerun of the suite with a dummy provider key (any live
  call fails), which makes no call and writes a new report, then `pinned.json` names it. Such a
  report shows no recorded keys and no live tokens, which tells it apart from the run that made
  the recordings. A change that makes a recording miss (a prompt, a model, a tool schema) can't
  be re-pinned without calls: it needs the recording runs of ADR-025's merge gate first. A new
  case is recorded first, then pinned the same way. The README's "Running the checks" says how.

### Tests (each with a negative control)
| Test | Negative control |
|---|---|
| Replay with `--expect` and the committed reports passes for every pinned suite | A pinned report with one verdict's detail changed fails, naming that case-run |
| A planted change to a turn prompt fails the job with a missing recording | Today's CI (no replay step) stays green on the same plant |
| A deleted recording fails the job, naming its key | — |
| A planted W01 that passes every verdict fails the job, listing the adversarial case-runs that moved to approved | Option B's rule (exit status only) stays green on the same plant |
| A case-run present on one side only fails, naming it | Comparing only the runs both sides have passes a deleted case |
| A suite folder missing from `pinned.json`, or a pinned report with errors, from a replay, or without buckets, is refused before any replay | Not validating the pin lets a new suite skip the gate |
| Run metadata (tokens, latency, commit, date, run id) never fails the comparison | Comparing the whole file fails on an unchanged suite |
| A planted bug in the split's summing fails the job on the totals, with every case-run equal | Comparing case-runs only stays green |
| A pinned suite whose folder is gone is refused | Checking one direction only keeps a pin that checks nothing |
| Recording errors and differences together exit 1 and print both | Returning on the first kind hides the other |
| A PR that changes the pin prints the case-runs that moved between the old and the new pinned report | — |

## Consequences

- Every PR that changes what the code decides shows it: the job lists the moved case-runs, and
  the PR's diff updates the pinned report. A reviewer sees "these three attacks now end in
  review" instead of a green check.
- Improvements also need a re-pin. That's deliberate: a fix is an outcome change too, and it
  should be visible.
- Re-pinning needs the database and a minute per suite locally; CI can't re-pin itself, by
  design (it holds no write access and no keys).
- The gate trusts determinism. If a non-deterministic judge arrives (an LLM judge, ADR-026's
  later decision), its verdicts are compared by rule or excluded from the pin; that's decided
  with the judge, not here.
- A careless re-pin is the one way a regression gets through, and it can't be done quietly: a
  hand-edited report doesn't help, because replay computes the real results and compares them,
  so passing means pinning a report that matches the new results, and the job prints which
  case-runs moved. What stops it is a reviewer reading that list.
- Two PRs that re-pin the same suite conflict in `pinned.json`; the second rebases and re-pins.
- What it doesn't do: say anything about today's model (that's the recording run and the later
  nightly job).
- **Revisit if:** re-pins become routine noise in most PRs (consider Option C for some fields),
  including re-pins forced only by a judge's wording, since detail strings are compared verbatim;
  a suite grows past what a per-case diff can be read for; or the nightly live job lands and
  needs its own thresholds.

## Interview-ready summary

"My replayed evals are deterministic, so instead of only failing CI when a recording is missing,
I pin every case's result to a committed report and fail on any difference. The recorded model
answers can't change in a PR, but the code that judges them can: loosen one rule and an attack
gets its refund back while every recording still replays, so an errors-only gate stays green.
I compare the totals too, because I once shipped a wrong number with no error anywhere. I
rejected one-sided thresholds on the safety counts, which is what you'd want for live, noisy
runs, because aggregates hide swaps and replay has no noise to tolerate. The cost is that
improvements need a re-pin too, but that re-pin is a reviewable diff showing which cases moved,
which is the point."

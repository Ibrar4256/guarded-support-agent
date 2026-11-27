"""Eval reports (ADR-026 S5): counts, never percentages, with the mode, model, k, anchor, commit
and date they were made under, plus tokens and latency from the case databases' ``llm_calls``.

Per category: cases that passed every one of their k runs (``25/28``) and case-runs that passed
(``80/84``). Per judge: case-runs where it passed. An errored run (a missing recording, an
invariant violation) counts as not passed and is listed with its error.

ADR-029 S4: the report names the warrant model and the purposes a record-missing run recorded,
counts keys recorded and replayed by purpose and live tokens by provider, and splits the case
runs by what stopped them (``split``), with the reviews the new layer caused by cause.
"""

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_testing.evals.clock import ANCHOR
from agent_testing.evals.runner import CaseRunResult, RunConfig, percentile
from agent_testing.evals.split import classify, reviews_by_cause, split


def summarize(
    results: Sequence[CaseRunResult], *, config: RunConfig, commit: str, made_on: str | None = None
) -> dict[str, Any]:
    by_case: dict[tuple[str, str], list[CaseRunResult]] = defaultdict(list)
    for result in results:
        by_case[(result.category, result.case_id)].append(result)
    categories: dict[str, dict[str, Any]] = {}
    for category in sorted({category for category, _ in by_case}):
        cases = [runs for (cat, _), runs in by_case.items() if cat == category]
        runs = [run for case_runs in cases for run in case_runs]
        categories[category] = {
            "cases": f"{sum(all(r.passed for r in rs) for rs in cases)}/{len(cases)}",
            "case_runs": f"{sum(r.passed for r in runs)}/{len(runs)}",
        }
    judges: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for result in results:
        for verdict in result.verdicts:
            judges[verdict.judge][0] += verdict.passed
            judges[verdict.judge][1] += 1
    latencies = [ms for result in results for ms in result.latencies_ms]
    return {
        "suite": config.suite,
        "mode": config.mode.value,
        "model": config.model,
        "warrant_model": config.warrant,
        "record_purposes": sorted(purpose.value for purpose in config.record_purposes),
        "k": config.k,
        "anchor": ANCHOR.isoformat(),
        "commit": commit,
        "date": made_on or datetime.now(UTC).date().isoformat(),
        "categories": categories,
        "judges": {name: f"{passed}/{total}" for name, (passed, total) in sorted(judges.items())},
        "errors": [
            {"case": r.case_id, "run": r.run_index, "error": r.error} for r in results if r.error
        ],
        "usage": {
            "calls": sum(r.calls for r in results),
            "input_tokens": sum(r.input_tokens for r in results),
            "output_tokens": sum(r.output_tokens for r in results),
            "latency_ms_p50": percentile(latencies, 0.5),
            "latency_ms_p95": percentile(latencies, 0.95),
            "live_tokens_by_provider": _summed([r.spent for r in results]),
        },
        "keys": {
            "recorded": _summed([r.recorded for r in results]),
            "replayed": _summed([r.replayed for r in results]),
        },
        "split": split(results),
        "reviews_by_cause": reviews_by_cause(results),
        "runs": [
            {
                "case": r.case_id,
                "category": r.category,
                "run": r.run_index,
                "passed": r.passed,
                "bucket": classify(r),
                "verdicts": [
                    {"judge": v.judge, "passed": v.passed, "detail": v.detail} for v in r.verdicts
                ],
            }
            for r in results
        ],
    }


def _summed(counts: Sequence[Mapping[str, int]]) -> dict[str, int]:
    total: dict[str, int] = defaultdict(int)
    for count in counts:
        for name, value in count.items():
            total[name] += value
    return dict(sorted(total.items()))


def render(summary: dict[str, Any]) -> str:
    recorded = ", ".join(summary["record_purposes"]) or "none"
    lines = [
        f"{summary['suite']}: mode {summary['mode']}, model {summary['model']}, warrant model "
        f"{summary['warrant_model']}, k={summary['k']}, anchor {summary['anchor']}, commit "
        f"{summary['commit']}, {summary['date']}",
        f"record-missing purposes: {recorded}; keys recorded {summary['keys']['recorded']}, "
        f"replayed {summary['keys']['replayed']}",
        "category: cases passing every run, case-runs passing",
    ]
    for category, counts in summary["categories"].items():
        lines.append(f"  {category}: {counts['cases']}, {counts['case_runs']}")
    lines.append("judges: case-runs passing")
    lines += [f"  {name}: {count}" for name, count in summary["judges"].items()]
    lines.append("split: case-runs by what stopped them")
    lines += [f"  {bucket}: {count}" for bucket, count in summary["split"].items()]
    lines.append("reviews the new layer caused, by cause (case-runs)")
    lines += [f"  {cause}: {count}" for cause, count in summary["reviews_by_cause"].items()]
    usage = summary["usage"]
    lines.append(
        f"usage: {usage['calls']} calls, {usage['input_tokens']} in / {usage['output_tokens']} "
        f"out tokens, latency p50 {usage['latency_ms_p50']} ms, p95 {usage['latency_ms_p95']} ms; "
        f"live tokens by provider {usage['live_tokens_by_provider']}"
    )
    for error in summary["errors"]:
        lines.append(f"ERROR {error['case']} run {error['run']}: {error['error']}")
    return "\n".join(lines)


def report_path(root: Path, summary: dict[str, Any], recording_run: str) -> Path:
    models = summary["model"]
    if summary["warrant_model"] != summary["model"]:
        models += f"+warrant_{summary['warrant_model']}"
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", models)
    return root / f"{summary['date']}_{summary['suite']}_{slug}_{recording_run}.json"

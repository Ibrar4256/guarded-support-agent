"""Eval reports (ADR-026 S5): counts, never percentages, with the mode, model, k, anchor, commit
and date they were made under, plus tokens and latency from the case databases' ``llm_calls``.

Per category: cases that passed every one of their k runs (``25/28``) and case-runs that passed
(``80/84``). Per judge: case-runs where it passed. An errored run (a missing recording, an
invariant violation) counts as not passed and is listed with its error.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_testing.evals.clock import ANCHOR
from agent_testing.evals.runner import CaseRunResult, RunConfig, percentile


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
        },
        "runs": [
            {
                "case": r.case_id,
                "category": r.category,
                "run": r.run_index,
                "passed": r.passed,
                "verdicts": [
                    {"judge": v.judge, "passed": v.passed, "detail": v.detail} for v in r.verdicts
                ],
            }
            for r in results
        ],
    }


def render(summary: dict[str, Any]) -> str:
    lines = [
        f"{summary['suite']}: mode {summary['mode']}, model {summary['model']}, k={summary['k']}, "
        f"anchor {summary['anchor']}, commit {summary['commit']}, {summary['date']}",
        "category: cases passing every run, case-runs passing",
    ]
    for category, counts in summary["categories"].items():
        lines.append(f"  {category}: {counts['cases']}, {counts['case_runs']}")
    lines.append("judges: case-runs passing")
    lines += [f"  {name}: {count}" for name, count in summary["judges"].items()]
    usage = summary["usage"]
    lines.append(
        f"usage: {usage['calls']} calls, {usage['input_tokens']} in / {usage['output_tokens']} "
        f"out tokens, latency p50 {usage['latency_ms_p50']} ms, p95 {usage['latency_ms_p95']} ms"
    )
    for error in summary["errors"]:
        lines.append(f"ERROR {error['case']} run {error['run']}: {error['error']}")
    return "\n".join(lines)


def report_path(root: Path, summary: dict[str, Any], recording_run: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", summary["model"])
    return root / f"{summary['date']}_{summary['suite']}_{slug}_{recording_run}.json"

"""``python -m agent_testing.evals``: run a suite of eval cases (ADR-026 S1).

    python -m agent_testing.evals --suite smoke --mode replay|record|live \\
        --model groq:openai/gpt-oss-120b [--k 3] [--case <id> ...]

Needs the agent database settings and the Postgres superuser (it clones a database per case
run); ``record`` and ``live`` need the provider's key, ``replay`` makes no request. Refuses
``APP_ENV=production``. A recording run writes its report under ``services/agent/evals/reports``;
replay and live runs only print. Exits 1 if any case run errored (a missing recording, a real
date in a prompt, a timestamp outside the anchor's range); judges' verdicts are results, not
errors.

A recording run is budgeted and resumable (ADR-026 S6): it needs its provider in
``EVAL_PROVIDER_DAILY_TOKENS`` and ``EVAL_DEMO_RESERVE_TOKENS``, keeps a progress file under
``services/agent/evals/progress``, and exits 3 when it stops early (the day's budget is spent, or
a call went uncounted); the same command resumes it, and the report is written once every case
run has finished.
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent.llm.types import Purpose
from agent.turn.fingerprint import fingerprint
from agent_testing.evals.budget import (
    BudgetRefused,
    budget_from_env,
    exclusive,
    load_or_start,
    progress_key,
    record_suite,
    run_identity,
    today,
)
from agent_testing.evals.cases import Case, load_suite
from agent_testing.evals.databases import EvalDatabases
from agent_testing.evals.judges import check_args
from agent_testing.evals.recording import Mode, refuse_production
from agent_testing.evals.report import render, report_path, summarize
from agent_testing.evals.runner import (
    EVALS,
    CaseRunResult,
    InnerFactory,
    RunConfig,
    commit_sha,
    real_model,
    run_suite,
)

STOPPED_EARLY = 3


def main(
    argv: Sequence[str], environ: Mapping[str, str], *, inner: InnerFactory = real_model
) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_testing.evals")
    parser.add_argument("--suite", required=True)
    parser.add_argument("--mode", choices=[m.value for m in Mode], required=True)
    parser.add_argument("--model", required=True, help="provider:model, for both purposes")
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument("--case", action="append", help="only these case ids")
    parser.add_argument("--cases-root", type=Path, default=EVALS / "cases")
    parser.add_argument("--recordings", type=Path, default=EVALS / "recordings")
    parser.add_argument("--progress", type=Path, default=EVALS / "progress")
    parser.add_argument("--reports", type=Path, default=EVALS / "reports")
    args = parser.parse_args(argv)
    refuse_production(environ)
    if args.k < 1:
        parser.error("--k must be at least 1")
    config = RunConfig(
        args.suite,
        Mode(args.mode),
        args.model,
        args.k,
        cases_root=args.cases_root,
        recordings=args.recordings,
    )
    cases = load_suite(config.cases_root, config.suite)
    if args.case:
        unknown = set(args.case) - {case.id for case in cases}
        if unknown:
            parser.error(f"no such cases in {config.suite}: {sorted(unknown)}")
        cases = [case for case in cases if case.id in args.case]
    for case in cases:
        check_args(case)
    if config.mode is Mode.RECORD:
        try:
            return _record(
                cases, config, environ, progress=args.progress, reports=args.reports, inner=inner
            )
        except BudgetRefused as error:
            print(f"refused: {error}", file=sys.stderr)
            return 2
    databases = EvalDatabases.from_env(environ)
    recording_run = uuid.uuid4().hex[:12]
    commit = commit_sha()
    with databases.hold():
        databases.drop_abandoned()  # crashed runners' databases; live ones hold their lock
        databases.create_template(environ)
        try:
            results = asyncio.run(
                run_suite(
                    cases,
                    config=config,
                    environ=environ,
                    databases=databases,
                    recording_run=recording_run,
                    inner=inner,
                )
            )
        finally:
            databases.drop(databases.template)
    summary = summarize(results, config=config, commit=commit)
    print(render(summary))
    return 1 if summary["errors"] else 0


def _record(
    cases: Sequence[Case],
    config: RunConfig,
    environ: Mapping[str, str],
    *,
    progress: Path,
    reports: Path,
    inner: InnerFactory,
) -> int:
    budget = budget_from_env(environ, config.model.partition(":")[0])  # before any work
    identity = run_identity(
        suite=config.suite,
        model=config.model,
        k=config.k,
        case_files={
            case.id: (config.cases_root / config.suite / f"{case.id}.yaml").read_bytes()
            for case in cases
        },
        fingerprints={purpose.value: fingerprint(purpose) for purpose in Purpose},
    )
    # Read before the run: a recording run's own files would mark the tree dirty afterwards.
    commit = commit_sha()
    with exclusive(progress / f"{progress_key(identity)}.json"):
        state, resumed = load_or_start(progress, identity, budget.provider, today())
        state.resumed_at(commit)
        pending = state.pending([case.id for case in cases], config.k)
        print(
            f"recording run {state.recording_run} ({'resumed' if resumed else 'new'}): "
            f"{len(pending)} case runs to go; {budget.provider} has used {state.used()} of "
            f"{budget.tokens} tokens on {state.day} (daily {budget.daily}, reserve "
            f"{budget.reserve})"
        )
        databases = EvalDatabases.from_env(environ)
        with databases.hold():
            databases.drop_abandoned()
            databases.create_template(environ)
            try:
                stopped = asyncio.run(
                    record_suite(
                        cases,
                        config=config,
                        environ=environ,
                        databases=databases,
                        progress=state,
                        budget=budget,
                        inner=inner,
                    )
                )
            finally:
                databases.drop(databases.template)
        summary = summarize(
            _in_case_order(state.results, cases), config=config, commit=",".join(state.commits)
        )
        print(render(summary))
        if stopped is not None:
            print(f"stopped early: {stopped}; progress: {state.path}")
            return STOPPED_EARLY
        path = report_path(reports, summary, state.recording_run)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"report: {path}")
        state.path.unlink()  # finished: the report is the record now
    return 1 if summary["errors"] else 0


def _in_case_order(results: Sequence[CaseRunResult], cases: Sequence[Case]) -> list[CaseRunResult]:
    order = {case.id: n for n, case in enumerate(cases)}
    return sorted(results, key=lambda r: (order[r.case_id], r.run_index))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))

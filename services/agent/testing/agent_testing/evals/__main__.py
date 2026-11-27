"""``python -m agent_testing.evals``: run a suite of eval cases (ADR-026 S1).

    python -m agent_testing.evals --suite smoke --mode replay|record|live|record-missing \\
        --model groq:openai/gpt-oss-120b [--warrant-model provider:model] \\
        [--record-purposes warrant,summary] [--k 3] [--case <id> ...]

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

``--warrant-model`` gives the warrant check its own model (default: ``--model``). A
``record-missing`` run (ADR-029 S4) is budgeted and resumable like a recording run: it replays
every key it has and calls live only for missing keys of ``--record-purposes`` (default
``warrant,summary``; never ``turn``, so a changed turn prompt fails as in replay). Every provider
it may call live needs its budget entry.

``replay --expect <report>`` (ADR-030) compares the replay with a pinned report, case-run by
case-run and in the totals, and prints every difference; it exits 4 when they differ, and 1 on a
recording error, printing the differences too.
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent.llm.types import Purpose
from agent.turn.fingerprint import fingerprint
from agent_testing.evals.budget import (
    BudgetRefused,
    budgets_from_env,
    exclusive,
    live_providers,
    load_or_start,
    progress_key,
    record_suite,
    run_identity,
    today,
)
from agent_testing.evals.cases import Case, load_suite
from agent_testing.evals.databases import EvalDatabases
from agent_testing.evals.judges import check_args
from agent_testing.evals.pin import DIFFERENT, compare
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
MAX_PAUSE_SECONDS = 600  # a pause longer than a provider's minute window buys nothing more
RECORDING_MODES = (Mode.RECORD, Mode.RECORD_MISSING)
DEFAULT_RECORD_PURPOSES = frozenset({Purpose.WARRANT, Purpose.SUMMARY})


def main(
    argv: Sequence[str], environ: Mapping[str, str], *, inner: InnerFactory = real_model
) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_testing.evals")
    parser.add_argument("--suite", required=True)
    parser.add_argument("--mode", choices=[m.value for m in Mode], required=True)
    parser.add_argument("--model", required=True, help="provider:model, the turn and summaries")
    parser.add_argument(
        "--warrant-model", help="provider:model for the warrant check (default: --model)"
    )
    parser.add_argument(
        "--record-purposes",
        help="record-missing: comma-separated purposes to record (default: warrant,summary)",
    )
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument("--case", action="append", help="only these case ids")
    parser.add_argument("--cases-root", type=Path, default=EVALS / "cases")
    parser.add_argument("--recordings", type=Path, default=EVALS / "recordings")
    parser.add_argument("--progress", type=Path, default=EVALS / "progress")
    parser.add_argument("--reports", type=Path, default=EVALS / "reports")
    parser.add_argument(
        "--pause",
        type=float,
        default=0.0,
        help="record, record-missing: seconds before every turn after the first",
    )
    parser.add_argument(
        "--expect", type=Path, help="replay: the pinned report this replay must reproduce"
    )
    args = parser.parse_args(argv)
    refuse_production(environ)
    if args.k < 1:
        parser.error("--k must be at least 1")
    if not 0 <= args.pause <= MAX_PAUSE_SECONDS:
        parser.error(f"--pause must be between 0 and {MAX_PAUSE_SECONDS} seconds")
    mode = Mode(args.mode)
    expected = _expected(parser, args, mode)
    if args.pause and mode not in RECORDING_MODES:
        parser.error("--pause applies to record and record-missing only")
    if args.record_purposes is not None and mode is not Mode.RECORD_MISSING:
        parser.error("--record-purposes applies to record-missing mode only")
    record_purposes = frozenset[Purpose]()
    if mode is Mode.RECORD_MISSING:
        record_purposes = (
            DEFAULT_RECORD_PURPOSES
            if args.record_purposes is None
            else _purposes(parser, args.record_purposes)
        )
    config = RunConfig(
        args.suite,
        mode,
        args.model,
        args.k,
        cases_root=args.cases_root,
        recordings=args.recordings,
        pause=args.pause,
        warrant_model=args.warrant_model,
        record_purposes=record_purposes,
    )
    cases = load_suite(config.cases_root, config.suite)
    if args.case:
        unknown = set(args.case) - {case.id for case in cases}
        if unknown:
            parser.error(f"no such cases in {config.suite}: {sorted(unknown)}")
        cases = [case for case in cases if case.id in args.case]
    for case in cases:
        check_args(case)
    if config.mode in RECORDING_MODES:
        try:
            return _record(
                cases,
                config,
                environ,
                progress=args.progress,
                reports=args.reports,
                pause=args.pause,
                inner=inner,
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
    differences = [] if expected is None else compare(expected, summary)
    if expected is not None:
        print(
            "differences from the pinned report:\n" + "\n".join(f"  {d}" for d in differences)
            if differences
            else "no differences from the pinned report"
        )
    if summary["errors"]:
        return 1
    return DIFFERENT if differences else 0


def _record(
    cases: Sequence[Case],
    config: RunConfig,
    environ: Mapping[str, str],
    *,
    progress: Path,
    reports: Path,
    pause: float,
    inner: InnerFactory,
) -> int:
    budgets = budgets_from_env(environ, live_providers(config))  # before any work
    identity = run_identity(
        suite=config.suite,
        model=config.model,
        k=config.k,
        case_files={
            case.id: (config.cases_root / config.suite / f"{case.id}.yaml").read_bytes()
            for case in cases
        },
        fingerprints={purpose.value: fingerprint(purpose) for purpose in Purpose},
        warrant_model=config.warrant,
        mode=config.mode.value,
        record_purposes=[purpose.value for purpose in config.record_purposes],
    )
    # Read before the run: a recording run's own files would mark the tree dirty afterwards.
    commit = commit_sha()
    with exclusive(progress / f"{progress_key(identity)}.json"):
        state, resumed = load_or_start(progress, identity, tuple(budgets), today())
        state.resumed_at(commit)
        pending = state.pending([case.id for case in cases], config.k)
        used = "; ".join(
            f"{name} has used {state.used(name)} of {budget.tokens} tokens (daily "
            f"{budget.daily}, reserve {budget.reserve})"
            for name, budget in budgets.items()
        )
        print(
            f"recording run {state.recording_run} ({'resumed' if resumed else 'new'}): "
            f"{len(pending)} case runs to go; on {state.day} {used}"
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
                        budgets=budgets,
                        inner=inner,
                        pause=pause,
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


def _purposes(parser: argparse.ArgumentParser, raw: str) -> frozenset[Purpose]:
    known = {purpose.value: purpose for purpose in Purpose}
    chosen = set()
    for name in (item.strip() for item in raw.split(",")):
        if name not in known:
            parser.error(f"--record-purposes: unknown purpose {name!r} (known: {sorted(known)})")
        chosen.add(known[name])
    if Purpose.TURN in chosen:
        parser.error(
            "--record-purposes: turn calls are never recorded by record-missing; a changed turn "
            "prompt must fail as in replay (use record mode to re-record)"
        )
    return frozenset(chosen)


def _expected(
    parser: argparse.ArgumentParser, args: argparse.Namespace, mode: Mode
) -> dict[str, Any] | None:
    """The pinned report ``--expect`` names, refused before any work if it can't apply."""
    if args.expect is None:
        return None
    if mode is not Mode.REPLAY:
        parser.error("--expect applies to replay mode only")
    try:
        expected: dict[str, Any] = json.loads(args.expect.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        parser.error(f"--expect: can't read {args.expect} ({error})")
    if expected.get("suite") != args.suite:
        parser.error(
            f"--expect: {args.expect} is a report of {expected.get('suite')}, not {args.suite}"
        )
    if expected.get("k") != args.k:
        parser.error(f"--k {args.k}, but the pinned report has k={expected.get('k')}")
    return expected


def _in_case_order(results: Sequence[CaseRunResult], cases: Sequence[Case]) -> list[CaseRunResult]:
    order = {case.id: n for n, case in enumerate(cases)}
    return sorted(results, key=lambda r: (order[r.case_id], r.run_index))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))

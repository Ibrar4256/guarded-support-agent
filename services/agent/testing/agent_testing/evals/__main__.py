"""``python -m agent_testing.evals``: run a suite of eval cases (ADR-026 S1).

    python -m agent_testing.evals --suite smoke --mode replay|record|live \\
        --model groq:openai/gpt-oss-120b [--k 3] [--case <id> ...]

Needs the agent database settings and the Postgres superuser (it clones a database per case
run); ``record`` and ``live`` need the provider's key, ``replay`` makes no request. Refuses
``APP_ENV=production``. A recording run writes its report under ``services/agent/evals/reports``;
replay and live runs only print. Exits 1 if any case run errored (a missing recording, a real
date in a prompt, a timestamp outside the anchor's range); judges' verdicts are results, not
errors.
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_testing.evals.cases import load_suite
from agent_testing.evals.databases import EvalDatabases
from agent_testing.evals.judges import check_args
from agent_testing.evals.recording import Mode, refuse_production
from agent_testing.evals.report import render, report_path, summarize
from agent_testing.evals.runner import EVALS, RunConfig, commit_sha, run_suite


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_testing.evals")
    parser.add_argument("--suite", required=True)
    parser.add_argument("--mode", choices=[m.value for m in Mode], required=True)
    parser.add_argument("--model", required=True, help="provider:model, for both purposes")
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument("--case", action="append", help="only these case ids")
    parser.add_argument("--cases-root", type=Path, default=EVALS / "cases")
    parser.add_argument("--recordings", type=Path, default=EVALS / "recordings")
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
    databases = EvalDatabases.from_env(environ)
    recording_run = uuid.uuid4().hex[:12]
    # Read before the run: a recording run's own files would mark the tree dirty afterwards.
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
                )
            )
        finally:
            databases.drop(databases.template)
    summary = summarize(results, config=config, commit=commit)
    print(render(summary))
    if config.mode is Mode.RECORD:
        path = report_path(EVALS / "reports", summary, recording_run)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"report: {path}")
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))

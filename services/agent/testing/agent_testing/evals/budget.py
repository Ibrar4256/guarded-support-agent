"""A recording run's token budget and progress file (ADR-026 S6, ADR-025 S7).

The budget for a provider is ``EVAL_PROVIDER_DAILY_TOKENS - EVAL_DEMO_RESERVE_TOKENS`` (each a
comma-separated ``provider:tokens`` list); a run refuses to start when that is not positive, or
when a provider it may call live has no entry (ADR-029 S4: the turn's and the warrant model's in
record mode, those of the recorded purposes in record-missing). The progress file, keyed by
everything that decides the run's results (suite, model, warrant model, mode, recorded purposes,
k, the case files' contents, the prompt fingerprints), records the finished case runs and the
tokens charged to each provider on the current UTC day: what each case run spent live with it
(the ``EvalModel``'s tally; a replayed call costs nothing). Re-running the same command resumes
it: finished case runs are skipped, the counter resets when the day changes, and the run stops
between case runs once any provider's budget for the day is spent. A case run with a call the
provider didn't fully count can't be charged honestly, so the run stops after it; one with a
failed provider call (a rate limit, an outage) is charged but stays unfinished, and the run
stops so the resume runs it again.
"""

import asyncio
import fcntl
import hashlib
import json
import os
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.llm.providers import PROVIDERS
from agent.llm.types import Purpose
from agent_testing.evals.cases import Case
from agent_testing.evals.databases import EvalDatabases
from agent_testing.evals.recording import Mode, canonical
from agent_testing.evals.runner import CaseRunResult, InnerFactory, RunConfig, real_model, run_case

DAILY_VAR = "EVAL_PROVIDER_DAILY_TOKENS"
RESERVE_VAR = "EVAL_DEMO_RESERVE_TOKENS"
VERSION = 2  # 2: charged per provider from the live tally (ADR-029 S4)
KEY_CHARS = 16
# What the pause between case runs awaits; a test seam.
sleep = asyncio.sleep


class BudgetRefused(ValueError):
    pass


def token_map(environ: Mapping[str, str], name: str) -> dict[str, int]:
    """``provider:tokens,...``: known providers, each once, tokens a non-negative integer."""
    found: dict[str, int] = {}
    raw = environ.get(name, "").strip()
    if not raw:
        return found
    for item in raw.split(","):
        provider, sep, tokens = item.strip().partition(":")
        if not sep or provider not in PROVIDERS:
            raise BudgetRefused(f"{name}: {item.strip()!r} is not known_provider:tokens")
        if provider in found:
            raise BudgetRefused(f"{name}: {provider} is listed twice")
        if not tokens.isdigit():
            raise BudgetRefused(f"{name}: {provider}'s tokens must be a whole number")
        found[provider] = int(tokens)
    return found


@dataclass(frozen=True)
class Budget:
    provider: str
    daily: int
    reserve: int

    @property
    def tokens(self) -> int:
        return self.daily - self.reserve


def budget_from_env(environ: Mapping[str, str], provider: str) -> Budget:
    daily = token_map(environ, DAILY_VAR).get(provider)
    reserve = token_map(environ, RESERVE_VAR).get(provider)
    if daily is None or reserve is None:
        raise BudgetRefused(
            f"a recording run on {provider} needs {provider} in {DAILY_VAR} and {RESERVE_VAR}"
        )
    budget = Budget(provider, daily, reserve)
    if budget.tokens <= 0:
        raise BudgetRefused(
            f"{provider}'s budget would cut into the demo reserve: daily {daily} - reserve "
            f"{reserve} leaves nothing to record with"
        )
    return budget


def budgets_from_env(environ: Mapping[str, str], providers: Sequence[str]) -> dict[str, Budget]:
    return {provider: budget_from_env(environ, provider) for provider in providers}


def live_providers(config: RunConfig) -> tuple[str, ...]:
    """Every provider the run may call live, sorted; each needs a budget."""
    by_purpose = {
        Purpose.TURN: config.model,
        Purpose.SUMMARY: config.model,
        Purpose.WARRANT: config.warrant,
    }
    if config.mode is Mode.RECORD_MISSING:
        labels = {by_purpose[purpose] for purpose in config.record_purposes}
    elif config.mode is Mode.REPLAY:
        labels = set()
    else:
        labels = set(by_purpose.values())
    return tuple(sorted({label.partition(":")[0] for label in labels}))


def today() -> str:
    return datetime.now(UTC).date().isoformat()


def run_identity(
    *,
    suite: str,
    model: str,
    k: int,
    case_files: Mapping[str, bytes],
    fingerprints: Mapping[str, str],
    warrant_model: str,
    mode: str,
    record_purposes: Sequence[str],
) -> dict[str, Any]:
    """What decides a recording run's results; a change starts a new run, never a mixed one."""
    return {
        "suite": suite,
        "model": model,
        "warrant_model": warrant_model,
        "mode": mode,
        "record_purposes": sorted(record_purposes),
        "k": k,
        "cases": {
            case_id: hashlib.sha256(data).hexdigest()
            for case_id, data in sorted(case_files.items())
        },
        "fingerprints": dict(sorted(fingerprints.items())),
    }


def progress_key(identity: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(identity).encode("utf-8")).hexdigest()[:KEY_CHARS]


@dataclass
class Progress:
    path: Path
    identity: dict[str, Any]
    providers: tuple[str, ...]  # the budgeted ones
    recording_run: str
    day: str
    tokens_used_today: dict[str, int] = field(default_factory=dict)
    results: list[CaseRunResult] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)  # every commit the run was resumed at

    @property
    def finished(self) -> set[tuple[str, int]]:
        return {(r.case_id, r.run_index) for r in self.results}

    def used(self, provider: str) -> int:
        return self.tokens_used_today.get(provider, 0)

    def exhausted(self, budgets: Mapping[str, Budget]) -> str | None:
        """Why the run must stop for the day, or None while every budget has tokens left."""
        for provider, budget in sorted(budgets.items()):
            if self.used(provider) >= budget.tokens:
                return (
                    f"{provider}'s budget for {self.day} is spent ({self.used(provider)} of "
                    f"{budget.tokens} tokens); run the same command tomorrow to resume"
                )
        return None

    def roll_day(self, day: str) -> None:
        if day != self.day:
            self.day = day
            self.tokens_used_today = {}

    def charge(self, result: CaseRunResult) -> None:
        for provider, spent in result.spent.items():
            self.tokens_used_today[provider] = self.used(provider) + spent

    def record(self, result: CaseRunResult) -> None:
        """A finished case run: charged, and skipped from now on."""
        self.charge(result)
        self.results.append(result)

    def resumed_at(self, commit: str) -> None:
        if commit not in self.commits:
            self.commits.append(commit)

    def pending(self, case_ids: Sequence[str], k: int) -> list[tuple[str, int]]:
        done = self.finished
        return [(c, i) for c in case_ids for i in range(1, k + 1) if (c, i) not in done]

    def to_json(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "identity": self.identity,
            "providers": list(self.providers),
            "recording_run": self.recording_run,
            "day": self.day,
            "tokens_used_today": self.tokens_used_today,
            "finished": sorted([c, i] for c, i in self.finished),
            "results": [r.to_json() for r in self.results],
            "commits": self.commits,
        }

    def save(self) -> None:
        """Write a temporary file and rename it over the old one: a crash leaves one or the
        other, never half a file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)


def load_or_start(
    root: Path, identity: dict[str, Any], providers: Sequence[str], day: str
) -> tuple[Progress, bool]:
    """The run's progress (resumed: True) or a new one with a fresh recording run id."""
    providers = tuple(providers)
    path = root / f"{progress_key(identity)}.json"
    if not path.is_file():
        progress = Progress(path, identity, providers, uuid.uuid4().hex[:12], day)
        return progress, False
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != VERSION:
        raise BudgetRefused(
            f"{path}: progress file version {data.get('version')!r} isn't {VERSION}"
        )
    if data["identity"] != identity or tuple(data["providers"]) != providers:
        raise BudgetRefused(f"{path} belongs to another run; move it aside to start this one")
    results = [CaseRunResult.from_json(item) for item in data["results"]]
    if sorted([r.case_id, r.run_index] for r in results) != data["finished"]:
        raise BudgetRefused(f"{path}: its finished list and its results disagree")
    progress = Progress(
        path,
        identity,
        providers,
        data["recording_run"],
        data["day"],
        {name: int(tokens) for name, tokens in data["tokens_used_today"].items()},
        results,
        list(data["commits"]),
    )
    progress.roll_day(day)
    return progress, True


@contextmanager
def exclusive(path: Path) -> Iterator[None]:
    """One runner per progress file; the OS drops the lock if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BudgetRefused(f"another runner is recording {path.name}") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def record_suite(
    cases: Sequence[Case],
    *,
    config: RunConfig,
    environ: Mapping[str, str],
    databases: EvalDatabases,
    progress: Progress,
    budgets: Mapping[str, Budget],
    inner: InnerFactory = real_model,
    pause: float = 0.0,
) -> str | None:
    """Run the pending case runs, saving progress after each; why it stopped early, or None.

    ``pause`` seconds pass between case runs (never before the first); with the runner's
    pause between a case's turns (``RunConfig.pause``), every turn after the run's first waits,
    to stay under a provider's tokens-a-minute limit instead of meeting its 429s."""
    by_id = {case.id: case for case in cases}
    for number, (case_id, run_index) in enumerate(progress.pending(list(by_id), config.k)):
        stop = progress.exhausted(budgets)
        if stop is not None:
            return stop
        if number and pause:
            await sleep(pause)
        result = await run_case(
            by_id[case_id],
            run_index,
            config=config,
            environ=environ,
            databases=databases,
            recording_run=progress.recording_run,
            inner=inner,
        )
        if result.failed_calls:
            # A rate limit or outage isn't the model's answer: charge what it spent, keep the
            # case run pending so the resume runs it again.
            progress.charge(result)
            progress.save()
            return (
                f"{case_id} run {run_index} had {result.failed_calls} failed provider call(s); "
                "it stays unfinished: run the same command to retry it"
            )
        progress.record(result)
        progress.save()
        if result.uncounted_calls:
            return (
                f"{case_id} run {run_index} made {result.uncounted_calls} call(s) the provider "
                "didn't fully count, so the budget can't be kept: check the provider's usage, "
                "then run the same command to resume"
            )
    return None

"""Run a suite's cases through the real turn, each in its own database (ADR-026 Part 1, S1-S5).

Per case and run index: clone the template, seed the case at the anchor, send its messages one
turn at a time through ``run_turn`` with the logical clock, seeded ids and nonces and a
pool whose members wrap one ``EvalModel`` (the turn and summaries on ``--model``, the warrant
check on ``--warrant-model``, ADR-029 S4), wait for background summaries, check the timestamp
invariant (S2a), judge, read tokens and latency from ``llm_calls`` and what the proposals
decided from ``decisions`` (for the report's split), and drop the database. A recording error (a
missing recording, another anchor, a real date in a prompt) or an invariant violation is the
run's error, not a judge's verdict. What the run spent live comes from the ``EvalModel``'s tally,
kept even when the run errors.
"""

import asyncio
import dataclasses
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, create_engine, func, select
from sqlalchemy.ext.asyncio import create_async_engine

from agent.alerts import LogAlerter
from agent.db.llm_calls import DbCallRecorder
from agent.db.proposal import PROPOSAL_STAGE
from agent.db.schema import decisions, llm_calls, runs
from agent.llm.adapter import OpenAICompatibleModel
from agent.llm.errors import OUTCOME_OK
from agent.llm.pool import PoolMember, ProviderPool
from agent.llm.prices import PriceTable
from agent.llm.providers import PROVIDERS
from agent.llm.settings import LLMSettings, ProviderChoice
from agent.llm.types import (
    ChatModel,
    FinishReason,
    Message,
    ModelResponse,
    OnText,
    Purpose,
    ToolCall,
    ToolSchema,
    UsageCompleteness,
)
from agent.policy_settings import policy_config_from_env
from agent.turn.fingerprint import fingerprint
from agent.turn.run import TurnDeps, run_turn
from agent.turn.settings import TurnSettings
from agent.turn.start import TurnIds
from agent_testing.env import credentials, example_llm_env, example_turn_env
from agent_testing.evals.cases import Case, ScriptedResponse
from agent_testing.evals.clock import LogicalClock, SeededIds, SeededNonces
from agent_testing.evals.databases import EvalDatabases
from agent_testing.evals.invariant import EXEMPT, timestamp_columns, violations
from agent_testing.evals.judges import CaseState, Verdict, judge
from agent_testing.evals.recording import (
    CaseRun,
    EvalModel,
    EvalRefused,
    Mode,
    RecordingError,
    RecordingStore,
    Route,
)
from agent_testing.evals.seed import seed

EVALS = Path(__file__).resolve().parents[3] / "evals"
REPO = EVALS.parents[2]
# Credentials the settings need to load in replay, where no request is ever made.
REPLAY_CREDENTIALS = {
    "CLOUDFLARE_ACCOUNT_ID": "0" * 32,
    "OLLAMA_BASE_URL": "http://localhost:11434/v1",
}
REPLAY_KEY = "replay-makes-no-request"
# A turn's own deadline plus room for its transactions; a summary's plus the same.
SLACK = timedelta(seconds=15)
# What the pause between turns awaits; a test seam.
sleep = asyncio.sleep


class ModelNeverCalled(EvalRefused):
    pass


class NoCalls:
    """Replay's inner model: reaching it is a bug (EvalModel answers from recordings)."""

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
        *,
        deadline: float,
        purpose: Purpose,
        on_text: OnText | None = None,
    ) -> ModelResponse:
        raise ModelNeverCalled("replay reached the real model")


@dataclass(frozen=True)
class RunConfig:
    suite: str
    mode: Mode
    model: str  # provider:model, the turn and summaries
    k: int = 1
    cases_root: Path = EVALS / "cases"
    recordings: Path = EVALS / "recordings"
    # Settings over the .env.example values (tests: a short window so summaries run).
    env_overrides: Mapping[str, str] = field(default_factory=dict)
    # Seconds before each of a case run's turns after its first: paces a provider's
    # tokens-a-minute limit without eating into a turn's own deadline.
    pause: float = 0.0
    # provider:model for the warrant check; None: ``model`` (ADR-029 S4).
    warrant_model: str | None = None
    # record-missing: the purposes whose missing keys are called live and recorded.
    record_purposes: frozenset[Purpose] = frozenset()

    @property
    def warrant(self) -> str:
        return self.model if self.warrant_model is None else self.warrant_model


@dataclass
class CaseRunResult:
    case_id: str
    category: str
    run_index: int
    verdicts: list[Verdict] = field(default_factory=list)
    error: str | None = None
    keys: list[str] = field(default_factory=list)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Calls whose usage the provider didn't fully report: their tokens are unknown (S6 budget).
    uncounted_calls: int = 0
    # Calls that failed at the provider (rate limit, outage): the run isn't the model's answer.
    failed_calls: int = 0
    latencies_ms: list[int] = field(default_factory=list)
    stamps: dict[str, list[str]] = field(default_factory=dict)  # for the replay tests
    # ADR-029 S4: tokens of live calls by provider (what the budget charges), and keys by purpose.
    spent: dict[str, int] = field(default_factory=dict)
    recorded: dict[str, int] = field(default_factory=dict)
    replayed: dict[str, int] = field(default_factory=dict)
    # For the report's split: the case's marker, any approval, and each proposal's decision
    # (outcome, rule ids, warrant status, C02 pattern ids).
    false_complaint: bool = False
    approved: bool = False
    proposals: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.error is None and all(v.passed for v in self.verdicts)

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "CaseRunResult":
        return cls(**{**data, "verdicts": [Verdict(**v) for v in data["verdicts"]]})


def scripted_responses(case: Case, provider: str, model: str) -> list[ModelResponse]:
    return [_scripted(index, item, provider, model) for index, item in enumerate(case.script)]


def _scripted(index: int, item: ScriptedResponse, provider: str, model: str) -> ModelResponse:
    calls = tuple(
        ToolCall(id=f"script_{index}_{n}", name=call.name, arguments=call.arguments)
        for n, call in enumerate(item.tool_calls)
    )
    return ModelResponse(
        text=item.text,
        tool_calls=calls,
        finish_reason=FinishReason.TOOL_CALLS if calls else FinishReason.STOP,
        usage=None,
        completeness=UsageCompleteness.NONE,
        provider=provider,
        model=model,
        attempts=1,
        prompt_chars=0,
        output_chars=len(item.text) + sum(len(c.name) + len(c.arguments) for c in calls),
        first_token_seconds=None,
        latency_seconds=0.0,
    )


def eval_env(environ: Mapping[str, str], config: RunConfig, runtime_url: str) -> dict[str, str]:
    env = {
        **example_turn_env(db_url=runtime_url),
        **example_llm_env(
            turn_models=config.model, summary_models=config.model, warrant_models=config.warrant
        ),
        **config.env_overrides,
    }
    if config.mode is Mode.REPLAY:
        for label in (config.model, config.warrant):
            provider = PROVIDERS[label.partition(":")[0]]
            for name in (provider.key_var, provider.url_var):
                if name is not None:
                    env[name] = REPLAY_CREDENTIALS.get(name, REPLAY_KEY)
    else:
        env.update(credentials(environ))
    return env


InnerFactory = Callable[[LLMSettings, ProviderChoice], ChatModel]
# A test seam: runs on the case database after the turns, before the invariant and judges.
BeforeJudging = Callable[[Connection, Sequence[TurnIds]], None]


def real_model(llm: LLMSettings, choice: ProviderChoice) -> ChatModel:
    return OpenAICompatibleModel(choice, llm)


async def run_case(
    case: Case,
    run_index: int,
    *,
    config: RunConfig,
    environ: Mapping[str, str],
    databases: EvalDatabases,
    recording_run: str,
    inner: InnerFactory = real_model,
    before_judging: BeforeJudging | None = None,
) -> CaseRunResult:
    result = CaseRunResult(case.id, case.category, run_index, false_complaint=case.false_complaint)
    model: EvalModel | None = None
    with databases.clone() as name:
        owner = create_engine(databases.owner(name))
        runtime = create_async_engine(databases.runtime(name))
        try:
            env = eval_env(environ, config, databases.runtime(name))
            llm = LLMSettings.from_env(env)
            settings = TurnSettings.from_env(env)
            choice = llm.models[Purpose.TURN][0]
            warrant = llm.models[Purpose.WARRANT][0]
            conversation = seed(owner, case)
            live: dict[str, ChatModel] = {}  # one model per provider:model

            def live_model(picked: ProviderChoice) -> ChatModel:
                if config.mode is Mode.REPLAY:
                    return NoCalls()
                if picked.label not in live:
                    live[picked.label] = inner(llm, picked)
                return live[picked.label]

            model = EvalModel(
                live_model(choice),
                mode=config.mode,
                store=RecordingStore(config.recordings),
                provider=choice.provider,
                model=choice.model,
                fingerprints={purpose: fingerprint(purpose) for purpose in Purpose},
                temperature=llm.temperature,
                max_output_tokens=llm.max_output_tokens,
                run=CaseRun(f"{case.suite}/{case.id}", run_index, recording_run),
                script=scripted_responses(case, choice.provider, choice.model),
                routes={
                    Purpose.WARRANT: Route(warrant.provider, warrant.model, live_model(warrant))
                },
                record_purposes=config.record_purposes,
            )
            alerter = LogAlerter(timedelta(seconds=60))
            member = PoolMember(choice.provider, choice.model, model)
            members = {
                Purpose.TURN: [member],
                Purpose.SUMMARY: [member],
                Purpose.WARRANT: [PoolMember(warrant.provider, warrant.model, model)],
            }
            seed_text = f"{case.suite}/{case.id}:{run_index}"
            deps = TurnDeps(
                engine=runtime,
                settings=settings,
                pool=ProviderPool(
                    members,
                    cooldown=llm.quota_cooldown,
                    alerter=alerter,
                ),
                record=DbCallRecorder(
                    runtime,
                    statement_timeout=settings.statement_timeout,
                    lock_timeout=settings.lock_timeout,
                ),
                prices=PriceTable.load(),
                alerter=alerter,
                policy=policy_config_from_env(env),
                clock=LogicalClock(),
                new_id=SeededIds(seed_text),
                nonce=SeededNonces(seed_text),
            )
            turns = await _send(deps, case, conversation, settings, pause=config.pause)
            if model.errors:  # caught by a best-effort caller (a background summary)
                raise model.errors[0]
            result.keys = list(model.keys)
            if before_judging is not None:
                with owner.begin() as conn:
                    before_judging(conn, turns)
            with owner.connect() as conn:
                broken = violations(conn, {"runs.review_deadline": settings.review_deadline})
                if broken:
                    result.error = "timestamp invariant: " + "; ".join(map(str, broken))
                result.verdicts = judge(CaseState(conn, conversation, turns), case.expect)
                _usage(conn, result)
                _decided(conn, result)
                result.stamps = _stamps(conn)
        except (RecordingError, TimeoutError) as error:
            result.error = f"{type(error).__name__}: {error}"
        finally:
            if model is not None:
                # Live calls only: a replayed call costs nothing, a scripted response is ours.
                result.spent = dict(model.spent)
                result.uncounted_calls = model.uncounted
                result.recorded, result.replayed = dict(model.recorded), dict(model.replayed)
            await runtime.dispose()
            owner.dispose()
    return result


async def _send(
    deps: TurnDeps,
    case: Case,
    conversation: uuid.UUID,
    settings: TurnSettings,
    *,
    pause: float = 0.0,
) -> list[TurnIds]:
    turns = []
    for index, message in enumerate(case.messages):
        if index and pause:
            await sleep(pause)
        async with asyncio.timeout((settings.deadline + SLACK).total_seconds()):
            outcome = await run_turn(
                deps, conversation_id=conversation, user=case.customer, message=message
            )
        turns.append(outcome.ids)
        await deps.background.drain((settings.summary_deadline + SLACK).total_seconds())
        if len(deps.background):
            raise TimeoutError("a background summary is still running after its bound")
    return turns


def _usage(conn: Any, result: CaseRunResult) -> None:
    row = conn.execute(
        select(
            func.count(),
            func.coalesce(func.sum(llm_calls.c.input_tokens), 0),
            func.coalesce(func.sum(llm_calls.c.output_tokens), 0),
            func.count().filter(llm_calls.c.outcome != OUTCOME_OK),
        )
    ).one()
    # Uncounted calls come from the live tally (``run_case``): a replayed row isn't a call.
    result.calls, result.input_tokens, result.output_tokens, result.failed_calls = row
    result.latencies_ms = list(conn.execute(select(llm_calls.c.latency_ms)).scalars())


def _decided(conn: Any, result: CaseRunResult) -> None:
    """Each proposal's decision, in order, and whether any run was approved (ADR-029 S4)."""
    rows = conn.execute(
        select(decisions.c.outcome, decisions.c.rule_ids, decisions.c.input_snapshot)
        .where(decisions.c.stage == PROPOSAL_STAGE)
        .order_by(decisions.c.id)
    )
    result.proposals = [
        {
            "outcome": outcome,
            "rule_ids": list(rule_ids),
            # A fail-closed decision's snapshot carries neither: the rules never ran.
            "warrant": snapshot.get("warrant", {}).get("status"),
            "c02": list(snapshot.get("injection_signals", {}).get("pattern_ids", [])),
        }
        for outcome, rule_ids, snapshot in rows
    ]
    approved = conn.execute(select(func.count()).where(runs.c.approved_at.is_not(None)))
    result.approved = approved.scalar_one() > 0


def _stamps(conn: Any) -> dict[str, list[str]]:
    """Every stamp the case wrote that the invariant covers, for comparing two runs."""
    found: dict[str, list[str]] = {}
    for table, column in timestamp_columns(conn):
        name = f"{table}.{column}"
        if name in EXEMPT:
            continue
        values = conn.exec_driver_sql(
            f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL ORDER BY 1'
        ).scalars()
        found[name] = [value.isoformat() for value in values]
    return found


async def run_suite(
    cases: Sequence[Case],
    *,
    config: RunConfig,
    environ: Mapping[str, str],
    databases: EvalDatabases,
    recording_run: str,
    inner: InnerFactory = real_model,
) -> list[CaseRunResult]:
    results = []
    for case in cases:
        for run_index in range(1, config.k + 1):
            results.append(
                await run_case(
                    case,
                    run_index,
                    config=config,
                    environ=environ,
                    databases=databases,
                    recording_run=recording_run,
                    inner=inner,
                )
            )
    return results


def commit_sha() -> str:
    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, check=False, timeout=10
        )
        return done.stdout.strip()

    sha = git("rev-parse", "--short", "HEAD") or "unknown"
    return f"{sha}+dirty" if git("status", "--porcelain") else sha


def percentile(values: Sequence[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]

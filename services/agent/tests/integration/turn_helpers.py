"""Turn settings shared by the turn's integration tests."""

from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING

from agent.turn.settings import TurnSettings

if TYPE_CHECKING:
    from agent_testing.fakes import RecordingAlerter
    from sqlalchemy.ext.asyncio import AsyncEngine

    from agent.core.policy import PolicyConfig
    from agent.llm.recording import CallRecord
    from agent.llm.types import ChatModel
    from agent.turn.run import TurnDeps

SETTINGS = TurnSettings(
    max_model_calls=6,
    max_malformed=2,
    deadline=timedelta(seconds=60),
    lease=timedelta(seconds=30),
    heartbeat=timedelta(seconds=10),
    statement_timeout=timedelta(seconds=5),
    lock_timeout=timedelta(seconds=2),
    window_messages=12,
    digest_tickets=5,
    message_max_chars=50,
    window_max_chars=24_000,
    provider_failure_alert_after=3,
    summary_trigger_messages=8,
    summary_deadline=timedelta(seconds=30),
    proposal_lock_attempts=3,
    review_deadline=timedelta(days=1),
)
FAST = TurnSettings(
    **{
        **SETTINGS.__dict__,
        "lease": timedelta(seconds=0.6),
        "heartbeat": timedelta(seconds=0.05),
        "statement_timeout": timedelta(seconds=0.5),
        "lock_timeout": timedelta(seconds=0.3),
        "deadline": timedelta(seconds=1),
        "proposal_lock_attempts": 1,
    }
)


def turn_deps(
    engine: "AsyncEngine",
    model: "ChatModel",
    *,
    alerter: "RecordingAlerter",
    records: "list[CallRecord]",
    settings: TurnSettings = SETTINGS,
    monotonic: "Callable[[], float] | None" = None,
    policy: "PolicyConfig | None" = None,
) -> "TurnDeps":
    """A turn wired to one scripted model, recording calls into ``records``."""
    import time

    from worker_harness import POLICY

    from agent.llm.pool import PoolMember, ProviderPool
    from agent.llm.prices import PriceTable
    from agent.turn.run import TurnDeps

    async def record(call: "CallRecord") -> None:
        records.append(call)

    clock = monotonic or time.monotonic
    pool = ProviderPool(
        PoolMember("scripted", "scripted-model", model),
        None,
        cooldown=timedelta(seconds=900),
        alerter=alerter,
        monotonic=clock,
    )
    prices = PriceTable.parse({"version": "test", "currency": "USD", "models": {}})
    return TurnDeps(
        engine=engine,
        settings=settings,
        pool=pool,
        record=record,
        prices=prices,
        alerter=alerter,
        policy=policy or POLICY,
        monotonic=clock,
    )

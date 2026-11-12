"""A worker context wired with fakes, for the worker's integration tests."""

from collections.abc import Iterable
from dataclasses import replace
from datetime import timedelta
from typing import Any

from agent_testing.fakes import RecordingAlerter, ScriptedRefundClient, config_result
from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.core.policy import PolicyConfig
from agent.core.refund_outcome import HttpResult, RetryPolicy
from agent.db import claims
from agent.db.claims import Claim
from agent.refund_client.config import RefundClientSettings
from agent.worker.context import WorkerContext
from agent.worker.gate import ConfigGate
from agent.worker.hooks import NO_FAULTS, WorkerHooks
from agent.worker.timings import WorkerTimings

POLICY = PolicyConfig(
    currency="USD",
    auto_approve_max_minor=5_000,
    auto_approve_budget_minor=10_000,
    per_customer_limit_minor=20_000,
    per_customer_window=timedelta(days=30),
    refund_window=timedelta(days=30),
    breaker_threshold_minor=100_000,
    pending_review_cap=3,
    daily_proposal_cap=5,
    global_review_queue_cap=50,
    decline_cooldown=timedelta(days=7),
)
RETRY = RetryPolicy(
    attempts=5,
    backoff_base=timedelta(seconds=0.5),
    backoff_cap=timedelta(seconds=8),
    client_timeout=timedelta(seconds=8),
)
TIMINGS = WorkerTimings(
    lease=timedelta(seconds=30),
    heartbeat=timedelta(seconds=5),
    liveness_margin=timedelta(seconds=0.5),
    claim_cooldown=timedelta(seconds=60),
    statement_timeout=timedelta(seconds=3),
    lock_timeout=timedelta(seconds=0.2),
    config_refresh=timedelta(seconds=30),
    config_staleness=timedelta(seconds=90),
    presend_max_attempts=3,
    lock_retry_max=2,
    release_streak_limit=3,
    max_failed_reads=3,
    poll=timedelta(seconds=0.05),
    shutdown_grace=timedelta(seconds=10),
    policy=RETRY,
)
SETTINGS = RefundClientSettings(
    base_url="http://refund-api",
    token="t" * 32,
    policy=RETRY,
    max_provider_window=timedelta(seconds=900),
    min_provider_budget=timedelta(seconds=1),
)
READ_WINDOW_MS = 300_000
FITS = config_result(window_ms=READ_WINDOW_MS, retention_ms=3_600_000)
BOUND = 5.0


class Harness:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        configs: Iterable[HttpResult] = (FITS,),
        sends: Iterable[HttpResult] = (),
        hooks: WorkerHooks = NO_FAULTS,
        timings: WorkerTimings = TIMINGS,
        client: ScriptedRefundClient | None = None,
    ) -> None:
        self.client = client or ScriptedRefundClient(configs=configs, sends=sends)
        self.alerter = RecordingAlerter()
        self.sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        self.ctx = WorkerContext(
            engine=engine,
            client=self.client,
            refund_settings=replace(SETTINGS, policy=timings.policy),
            timings=timings,
            policy=POLICY,
            gate=ConfigGate(
                worst_case=RETRY.worst_case_window(),
                staleness=timings.config_staleness,
                alerter=self.alerter,
            ),
            alerter=self.alerter,
            hooks=hooks,
            jitter=lambda: 0.5,
            sleep=sleep,
        )

    async def claim(self, worker: str = "A", lease: timedelta | None = None) -> Claim:
        claimed = await self.claim_or_none(worker, lease)
        assert claimed is not None
        return claimed

    async def claim_or_none(self, worker: str, lease: timedelta | None = None) -> Claim | None:
        timings = self.ctx.timings
        async with claims.worker_transaction(
            self.ctx.engine,
            statement_timeout=timings.statement_timeout,
            lock_timeout=timings.lock_timeout,
        ) as conn:
            return await claims.claim_run(conn, worker, lease or timings.lease)


def only(rows: list[Row[Any]], what: str) -> Row[Any]:
    assert len(rows) == 1, f"expected exactly one {what}, found {len(rows)}"
    return rows[0]

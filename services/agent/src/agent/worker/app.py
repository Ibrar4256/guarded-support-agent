"""Wiring the worker process (ADR-014 Part 1, Part 5, S7).

``main`` is shared by both entry points: production (``python -m agent.worker``, the no-op
hooks) and the crash-test build (``python -m agent_testing.worker``, which passes its own
hooks). Production code never imports test code (ADR-007).
"""

import asyncio
import logging
import signal
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import create_async_engine

from agent.alerts import LogAlerter
from agent.refund_client.http import HttpRefundClient
from agent.worker.context import WorkerContext
from agent.worker.gate import ConfigGate
from agent.worker.hooks import NO_FAULTS, WorkerHooks
from agent.worker.lease import new_worker_id
from agent.worker.loop import run_worker
from agent.worker.settings import CONFIG_ERRORS, WorkerSettings

logger = logging.getLogger("agent.worker")

EXIT_OK = 0
EXIT_CONFIG = 2
# Variables only the crash-test entry point honors (ADR-004, ADR-014 Part 5).
FAULT_VARIABLES = ("CRASH_POINT", "DEBUG_FAULT_INJECTION")
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


async def serve(settings: WorkerSettings, hooks: WorkerHooks = NO_FAULTS) -> None:
    """Run until SIGTERM or SIGINT. Raises ``WorkerConfigError`` if refund-api's window could
    never fit a retry schedule (ADR-012 Part 4)."""
    engine = create_async_engine(
        settings.db_url,
        pool_size=settings.pool_size,
        max_overflow=0,
        # Waiting for a pooled connection is bounded like any other database wait (S8).
        pool_timeout=settings.timings.statement_timeout.total_seconds(),
        connect_args=dict(settings.db_connect_args),
    )
    client = HttpRefundClient(settings.refund)
    alerter = LogAlerter(settings.alert_interval)
    ctx = WorkerContext(
        engine=engine,
        client=client,
        refund_settings=settings.refund,
        timings=settings.timings,
        policy=settings.policy,
        gate=ConfigGate(
            worst_case=settings.refund.policy.worst_case_window(),
            staleness=settings.timings.config_staleness,
            alerter=alerter,
        ),
        alerter=alerter,
        hooks=hooks,
    )
    loop = asyncio.get_running_loop()
    for stop_signal in STOP_SIGNALS:
        loop.add_signal_handler(stop_signal, ctx.stopping.set)
    try:
        await run_worker(ctx, new_worker_id())
    finally:
        for stop_signal in STOP_SIGNALS:
            loop.remove_signal_handler(stop_signal)
        await client.aclose()
        await engine.dispose()


def main(environ: Mapping[str, str], hooks: WorkerHooks = NO_FAULTS) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = WorkerSettings.from_env(environ)
        asyncio.run(serve(settings, hooks))
    except CONFIG_ERRORS as exc:
        logger.error("refusing to start: %s", exc)
        return EXIT_CONFIG
    logger.info("worker stopped")
    return EXIT_OK


def production_main(environ: Mapping[str, str]) -> int:
    """The production entry point honors no fault variables, so either one being set means the
    wrong entry point was deployed (ADR-014 Part 5): refuse rather than run without them."""
    present = [name for name in FAULT_VARIABLES if environ.get(name)]
    if present:
        logging.basicConfig(level=logging.INFO)
        logger.error(
            "refusing to start: %s set, but only python -m agent_testing.worker honors it",
            ", ".join(present),
        )
        return EXIT_CONFIG
    return main(environ)

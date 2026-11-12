"""What every worker step needs, wired once per process."""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncEngine

from agent.alerts import Alerter
from agent.core.policy import PolicyConfig
from agent.refund_client.config import RefundClientSettings
from agent.refund_client.http import RefundClient
from agent.worker.gate import ConfigGate
from agent.worker.hooks import NO_FAULTS, WorkerHooks
from agent.worker.timings import WorkerTimings


@dataclass(frozen=True)
class WorkerContext:
    engine: AsyncEngine
    client: RefundClient
    refund_settings: RefundClientSettings
    timings: WorkerTimings
    policy: PolicyConfig
    gate: ConfigGate
    alerter: Alerter
    hooks: WorkerHooks = NO_FAULTS
    # Injected so tests are deterministic and fast (ADR-007: randomness and time injected).
    jitter: Callable[[], float] = random.random
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    # S7: set on SIGTERM. Handlers check it at each step boundary and release the run.
    stopping: asyncio.Event = field(default_factory=asyncio.Event)

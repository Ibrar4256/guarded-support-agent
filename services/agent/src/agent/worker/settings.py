"""Everything the worker process reads from its environment, validated together at startup."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from agent import envvars
from agent.core.policy import ConfigError, PolicyConfig
from agent.policy_settings import PolicySettingsError, policy_config_from_env
from agent.refund_client.config import RefundClientConfigError, RefundClientSettings
from agent.worker.timings import WorkerConfigError, WorkerTimings

DB_URL = "AGENT_DB_RUNTIME_URL"
POOL_SIZE = "WORKER_DB_POOL_SIZE"
PREPARE_THRESHOLD = "AGENT_DB_PREPARE_THRESHOLD"
ALERT_RATE_LIMIT = "ALERT_RATE_LIMIT_SECONDS"

# The heartbeat renews on its own connection while the run's step holds another (ADR-014 S8).
MIN_POOL_SIZE = 2
PREPARE_OFF = "off"

# Every way a configuration can be refused: the process exits with a message, not a traceback.
CONFIG_ERRORS: tuple[type[Exception], ...] = (
    WorkerConfigError,
    RefundClientConfigError,
    PolicySettingsError,
    ConfigError,
)


@dataclass(frozen=True)
class WorkerSettings:
    db_url: str
    pool_size: int
    # psycopg's ``prepare_threshold`` (ADR-005: a config value, because PgBouncer in
    # transaction mode breaks prepared statements). Empty: the driver's default.
    db_connect_args: Mapping[str, Any]
    refund: RefundClientSettings
    timings: WorkerTimings
    policy: PolicyConfig
    alert_interval: timedelta

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "WorkerSettings":
        refund = RefundClientSettings.from_env(environ)
        pool_size = envvars.count(environ, POOL_SIZE, WorkerConfigError)
        if pool_size < MIN_POOL_SIZE:
            raise WorkerConfigError(
                f"{POOL_SIZE} must be at least {MIN_POOL_SIZE}: the heartbeat needs its own "
                "connection"
            )
        return cls(
            db_url=envvars.required(environ, DB_URL, WorkerConfigError),
            pool_size=pool_size,
            db_connect_args=_connect_args(environ.get(PREPARE_THRESHOLD, "")),
            refund=refund,
            timings=WorkerTimings.from_env(environ, refund.policy),
            policy=policy_config_from_env(environ),
            alert_interval=envvars.seconds(environ, ALERT_RATE_LIMIT, WorkerConfigError),
        )


def _connect_args(raw: str) -> Mapping[str, Any]:
    if not raw:
        return {}
    if raw == PREPARE_OFF:
        return {"prepare_threshold": None}
    try:
        return {"prepare_threshold": int(raw)}
    except ValueError as exc:
        raise WorkerConfigError(
            f"{PREPARE_THRESHOLD} must be a whole number or '{PREPARE_OFF}', got {raw!r}"
        ) from exc

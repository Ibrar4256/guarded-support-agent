"""The worker's settings and entry points (ADR-009 Config, ADR-014 Part 1, Part 5)."""

import uuid
from collections.abc import Mapping
from datetime import timedelta

import pytest
from agent_testing import worker as crash_worker
from agent_testing.env import example_worker_env
from agent_testing.worker import CRASH_EXIT_CODE, CrashHooks, FaultConfigError, hooks_from_env

from agent.core.policy import ConfigError
from agent.policy_settings import (
    AUTO_APPROVE_BUDGET,
    PER_CUSTOMER_WINDOW,
    PolicySettingsError,
    policy_config_from_env,
)
from agent.worker import app
from agent.worker.app import EXIT_CONFIG, production_main
from agent.worker.hooks import NO_FAULTS, WorkerHooks
from agent.worker.settings import POOL_SIZE, PREPARE_THRESHOLD, WorkerSettings
from agent.worker.timings import WorkerConfigError

ENV = example_worker_env()
POLICY_VARS = sorted(name for name in ENV if name.startswith("POLICY_"))


# --- the policy config (ADR-009: from settings, validated at startup) --------------------------


def test_the_policy_config_reads_every_threshold() -> None:
    config = policy_config_from_env(ENV)
    assert (config.currency, config.per_customer_limit_minor) == ("USD", 20_000)
    assert config.per_customer_window == timedelta(days=30)


@pytest.mark.parametrize("name", POLICY_VARS)
def test_a_missing_policy_setting_refuses_startup(name: str) -> None:
    with pytest.raises(PolicySettingsError, match=name):
        policy_config_from_env({k: v for k, v in ENV.items() if k != name})


def test_a_malformed_policy_setting_names_itself() -> None:
    with pytest.raises(PolicySettingsError, match=PER_CUSTOMER_WINDOW):
        policy_config_from_env({**ENV, PER_CUSTOMER_WINDOW: "30d"})


def test_an_inconsistent_policy_is_refused_by_the_core_validation() -> None:
    with pytest.raises(ConfigError, match="auto_approve_budget_minor"):
        policy_config_from_env({**ENV, AUTO_APPROVE_BUDGET: "100"})


# --- the worker settings bundle ----------------------------------------------------------------


def test_the_example_environment_is_a_valid_worker_configuration() -> None:
    settings = WorkerSettings.from_env(ENV)
    assert settings.timings.policy is settings.refund.policy, "one retry policy, not two"
    assert settings.db_connect_args == {}


def test_a_pool_of_one_is_refused() -> None:
    with pytest.raises(WorkerConfigError, match=POOL_SIZE):
        WorkerSettings.from_env({**ENV, POOL_SIZE: "1"})


@pytest.mark.parametrize(
    ("raw", "args"),
    [("", {}), ("off", {"prepare_threshold": None}), ("0", {"prepare_threshold": 0})],
)
def test_the_prepare_threshold(raw: str, args: Mapping[str, object]) -> None:
    assert WorkerSettings.from_env({**ENV, PREPARE_THRESHOLD: raw}).db_connect_args == args


def test_a_malformed_prepare_threshold_is_refused() -> None:
    with pytest.raises(WorkerConfigError, match=PREPARE_THRESHOLD):
        WorkerSettings.from_env({**ENV, PREPARE_THRESHOLD: "never"})


# --- the production entry point ----------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.calls: list[WorkerHooks] = []

    def __call__(self, environ: Mapping[str, str], hooks: WorkerHooks = NO_FAULTS) -> int:
        self.calls.append(hooks)
        return 0


@pytest.mark.parametrize("name", ["CRASH_POINT", "DEBUG_FAULT_INJECTION"])
def test_production_refuses_to_start_with_a_fault_variable_set(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    monkeypatch.setattr(app, "main", recorder)
    assert production_main({**ENV, name: "before_refund_call"}) == EXIT_CONFIG
    assert recorder.calls == [], "never started"


def test_negative_control_production_starts_with_them_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    monkeypatch.setattr(app, "main", recorder)
    assert production_main({**ENV, "CRASH_POINT": "", "DEBUG_FAULT_INJECTION": ""}) == 0
    assert recorder.calls == [NO_FAULTS]


def test_a_refused_configuration_exits_cleanly_without_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def must_not_serve(*args: object) -> None:
        raise AssertionError("served with a bad configuration")

    monkeypatch.setattr(app, "serve", must_not_serve)
    assert app.main({k: v for k, v in ENV.items() if k != "WORKER_LEASE_SECONDS"}) == EXIT_CONFIG


# --- the crash-test entry point (ADR-004, ADR-014 Part 5) --------------------------------------

FAULTS = {"DEBUG_FAULT_INJECTION": "true"}


def test_the_crash_worker_never_runs_in_production() -> None:
    with pytest.raises(FaultConfigError, match="production"):
        hooks_from_env({**FAULTS, "APP_ENV": "production", "CRASH_POINT": "before_refund_call"})


def test_the_crash_worker_needs_fault_injection_switched_on() -> None:
    with pytest.raises(FaultConfigError, match="DEBUG_FAULT_INJECTION"):
        hooks_from_env({"CRASH_POINT": "before_refund_call"})


def test_an_unknown_crash_point_is_refused() -> None:
    with pytest.raises(FaultConfigError, match="unknown CRASH_POINT"):
        hooks_from_env({**FAULTS, "CRASH_POINT": "somewhere"})


def test_negative_control_a_valid_crash_point_builds_crash_hooks() -> None:
    hooks = hooks_from_env({**FAULTS, "APP_ENV": "test", "CRASH_POINT": "after_refund_response"})
    assert isinstance(hooks, CrashHooks) and hooks.point == "after_refund_response"
    assert hooks_from_env(FAULTS) is NO_FAULTS


@pytest.mark.parametrize("point", sorted(crash_worker.CRASH_POINTS))
async def test_crash_hooks_exit_at_their_point_only(point: str) -> None:
    exits: list[int] = []
    hooks = CrashHooks(point, exit_now=exits.append)
    run = uuid.uuid4()
    await hooks.before_refund_call(run)
    assert exits == ([CRASH_EXIT_CODE] if point == "before_refund_call" else [])
    await hooks.after_refund_response(run)
    assert exits == [CRASH_EXIT_CODE], "exactly one exit, at the named point"


def test_the_crash_entry_point_refuses_before_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    monkeypatch.setattr(crash_worker, "main", recorder)
    assert crash_worker.crash_worker_main({**ENV, "APP_ENV": "production"}) == EXIT_CONFIG
    assert recorder.calls == []
    assert (
        crash_worker.crash_worker_main({**ENV, **FAULTS, "CRASH_POINT": "before_refund_call"}) == 0
    )
    [hooks] = recorder.calls
    assert isinstance(hooks, CrashHooks)

"""No secret in a repr (found 2026-11-20: a failing test printed the eval databases' URLs,
passwords included, and the refund token and the worker's database URL had the same
default repr). A traceback, a crash reporter or a failing test prints reprs; CI logs are
public. Every dataclass in the service and its test package whose field is named like a
secret keeps that field out of its repr."""

import dataclasses
import importlib
import pkgutil
import re
from types import ModuleType

import agent_testing
from agent_testing.env import example_worker_env

import agent
from agent.worker.settings import WorkerSettings

# A token or a password, not a count of tokens (``input_tokens``).
SECRET = re.compile(
    r"(^|_)(token|password|secret|api_key)$|^(tokens|secrets)$"
    r"|(^|_)(db_url|database_url|superuser_url|owner_url|runtime_url)$"
)


def dataclasses_in(package: ModuleType) -> list[type]:
    found = []
    for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        if info.name.endswith("__main__"):
            continue
        module = importlib.import_module(info.name)
        found += [
            obj
            for obj in vars(module).values()
            if isinstance(obj, type)
            and dataclasses.is_dataclass(obj)
            and obj.__module__ == module.__name__
        ]
    return found


def test_every_secret_field_is_out_of_its_dataclass_repr() -> None:
    shown = [
        f"{cls.__module__}.{cls.__qualname__}.{f.name}"
        for package in (agent, agent_testing)
        for cls in dataclasses_in(package)
        for f in dataclasses.fields(cls)
        if SECRET.search(f.name) and f.repr
    ]

    assert shown == []


def test_the_scan_finds_the_known_secret_fields() -> None:
    """Control: the scan reaches the classes that hold secrets."""
    names = {
        f"{cls.__qualname__}.{f.name}"
        for package in (agent, agent_testing)
        for cls in dataclasses_in(package)
        for f in dataclasses.fields(cls)
        if SECRET.search(f.name)
    }

    assert {
        "RefundClientSettings.token",
        "WorkerSettings.db_url",
        "ProviderChoice.api_key",
        "EvalDatabases.superuser_url",
    } <= names


def test_the_refund_token_and_database_url_are_not_in_the_settings_reprs() -> None:
    token = "refund-token-0123456789abcdef0123456789"
    env = example_worker_env(db_url="postgresql+psycopg://agent_runtime:db-secret-42@h:5433/agent")
    env["REFUND_API_TOKEN"] = token

    shown = repr(WorkerSettings.from_env(env))

    assert token not in shown and "db-secret-42" not in shown

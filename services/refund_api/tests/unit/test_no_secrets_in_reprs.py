"""No secret in a repr (found 2026-11-20 on the agent side: a failing test printed database
URLs with their passwords; this service's settings had the same default repr). A traceback,
a crash reporter or a failing test prints reprs; CI logs are public. Every dataclass in the
service and its test package whose field is named like a secret keeps it out of its repr.
The agent has its own copy of this check (ADR-007: no shared code)."""

import dataclasses
import importlib
import pkgutil
import re
from types import ModuleType

import refund_api_testing
from test_config import environ

import refund_api
from refund_api import config
from refund_api.config import Settings

# A token or a password, not a count of tokens.
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


def secret_fields() -> list[tuple[str, bool]]:
    return [
        (f"{cls.__qualname__}.{f.name}", f.repr)
        for package in (refund_api, refund_api_testing)
        for cls in dataclasses_in(package)
        for f in dataclasses.fields(cls)
        if SECRET.search(f.name)
    ]


def test_every_secret_field_is_out_of_its_dataclass_repr() -> None:
    assert [name for name, shown in secret_fields() if shown] == []


def test_the_scan_finds_the_known_secret_fields() -> None:
    """Control: the scan reaches the classes that hold secrets."""
    assert {"Settings.database_url", "Settings.tokens"} <= {name for name, _ in secret_fields()}


def test_the_tokens_and_database_url_are_not_in_the_settings_repr() -> None:
    token = "t" * config.MIN_TOKEN_LENGTH
    url = "postgresql+psycopg://refund_api_runtime:db-secret-42@h:5433/refund_api"

    shown = repr(
        Settings.from_env(environ(**{config.TOKEN_CURRENT: token, config.DATABASE_URL: url}))
    )

    assert token not in shown and "db-secret-42" not in shown

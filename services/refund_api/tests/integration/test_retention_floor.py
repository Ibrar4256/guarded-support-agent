"""The deploy-time floor command (ADR-010 Part 5, ADR-021): every deployment, no demo data."""

from collections.abc import Iterator
from datetime import timedelta

import pytest
from conftest import env, set_floor
from sqlalchemy import Engine, select

from refund_api.db.schema import key_retention_policy
from refund_api.retention_floor import main

RETENTION = "REFUND_API_KEY_RETENTION_SECONDS"
EARLIER = timedelta(hours=6)


@pytest.fixture
def deployment(owner_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A real deployment's environment: the owner URL and retention, no demo flag."""
    monkeypatch.setenv("REFUND_DB_OWNER_URL", env("REFUND_DB_OWNER_URL"))
    monkeypatch.delenv("DEMO_SEED_ENABLED", raising=False)
    yield
    set_floor(owner_engine, None)


def floor(engine: Engine) -> timedelta | None:
    with engine.connect() as connection:
        value: timedelta | None = connection.execute(
            select(key_retention_policy.c.min_retention)
        ).scalar_one_or_none()
    return value


@pytest.mark.usefixtures("deployment")
def test_sets_the_floor_without_the_demo_flag_or_a_manifest(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_floor(owner_engine, None)
    monkeypatch.setenv(RETENTION, "3600")
    assert main([]) == 0
    assert floor(owner_engine) == timedelta(hours=1)
    assert "key retention floor: 3600s" in capsys.readouterr().out


@pytest.mark.usefixtures("deployment")
@pytest.mark.parametrize("seconds", ["60", "86400"], ids=["lower", "higher"])
def test_a_re_run_replaces_the_floor(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch, seconds: str
) -> None:
    set_floor(owner_engine, EARLIER)
    monkeypatch.setenv(RETENTION, seconds)
    assert main([]) == 0
    assert floor(owner_engine) == timedelta(seconds=int(seconds))


@pytest.mark.usefixtures("deployment")
@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (None, "is not set"),
        ("", "is not set"),
        ("an hour", "must be a number of seconds"),
        ("0", "must be positive"),
        ("-60", "must be positive"),
    ],
    ids=["missing", "empty", "not-a-number", "zero", "negative"],
)
def test_a_bad_retention_is_refused_and_the_floor_kept(
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw: str | None,
    reason: str,
) -> None:
    set_floor(owner_engine, EARLIER)
    if raw is None:
        monkeypatch.delenv(RETENTION, raising=False)
    else:
        monkeypatch.setenv(RETENTION, raw)
    assert main([]) == 1
    err = capsys.readouterr().err
    assert "floor not set, nothing written" in err
    assert reason in err
    assert floor(owner_engine) == EARLIER


@pytest.mark.usefixtures("deployment")
def test_any_argument_is_a_usage_error(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_floor(owner_engine, EARLIER)
    monkeypatch.setenv(RETENTION, "60")
    assert main(["infra/seed/demo_manifest.json"]) == 2
    assert "usage: python -m refund_api.retention_floor" in capsys.readouterr().err
    assert floor(owner_engine) == EARLIER

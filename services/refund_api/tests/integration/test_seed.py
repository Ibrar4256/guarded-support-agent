"""The deploy-time seed (ADR-010 Parts 2 and 5): charges from the manifest, plus the floor."""

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import TOKEN, api_client, env, set_floor, unique
from sqlalchemy import Engine, select

from refund_api.db.schema import charges, key_retention_policy
from refund_api.seed import SeedError, main, seed

RETENTION_SECONDS = "3600"


@pytest.fixture
def environ(owner_engine: Engine) -> Iterator[dict[str, str]]:
    yield {
        "REFUND_DB_OWNER_URL": env("REFUND_DB_OWNER_URL"),
        "REFUND_API_KEY_RETENTION_SECONDS": RETENTION_SECONDS,
    }
    set_floor(owner_engine, None)


def order(order_id: str, charged_minor: int = 5_000, **extra: Any) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "owner_user_id": "user_seed",
        "charged_minor": charged_minor,
        "currency": "USD",
        "status": "delivered",
        "delivered_at": "2026-11-01T10:00:00Z",
        "chargeback_open": False,
        **extra,
    }


def manifest(tmp_path: Path, *orders: dict[str, Any]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"manifest_version": 1, "orders": list(orders)}), encoding="utf-8")
    return path


def charge_amounts(engine: Engine, *order_ids: str) -> dict[str, int]:
    with engine.connect() as connection:
        rows = connection.execute(select(charges).where(charges.c.order_id.in_(order_ids)))
        return {row.order_id: row.amount_minor for row in rows}


def floor(engine: Engine) -> timedelta | None:
    with engine.connect() as connection:
        value: timedelta | None = connection.execute(
            select(key_retention_policy.c.min_retention)
        ).scalar_one_or_none()
    return value


def test_seed_inserts_charges_and_sets_the_floor(
    tmp_path: Path, environ: dict[str, str], owner_engine: Engine
) -> None:
    a, b = unique("ord"), unique("ord")
    report = seed(manifest(tmp_path, order(a), order(b, 999)), environ)
    assert (report.inserted, report.unchanged) == (2, 0)
    assert charge_amounts(owner_engine, a, b) == {a: 5_000, b: 999}
    assert floor(owner_engine) == timedelta(seconds=int(RETENTION_SECONDS))


def test_seed_is_idempotent(tmp_path: Path, environ: dict[str, str]) -> None:
    path = manifest(tmp_path, order(unique("ord")))
    seed(path, environ)
    again = seed(path, environ)
    assert (again.inserted, again.unchanged) == (0, 1)


def test_a_mismatched_charge_refuses_everything(
    tmp_path: Path, environ: dict[str, str], owner_engine: Engine
) -> None:
    existing, new = unique("ord"), unique("ord")
    seed(manifest(tmp_path, order(existing, 5_000)), environ)
    with pytest.raises(SeedError, match=existing):
        seed(manifest(tmp_path, order(existing, 6_000), order(new)), environ)
    # Nothing written: the existing charge is unchanged and the new one wasn't inserted.
    assert charge_amounts(owner_engine, existing, new) == {existing: 5_000}


def test_cli_exit_codes(
    tmp_path: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    existing = unique("ord")
    assert main([str(manifest(tmp_path, order(existing)))]) == 0
    assert "charges inserted: 1" in capsys.readouterr().out
    assert main([str(manifest(tmp_path, order(existing, 1)))]) == 1
    assert "nothing written" in capsys.readouterr().err
    assert main([]) == 2


async def test_a_seeded_charge_can_be_refunded(tmp_path: Path, environ: dict[str, str]) -> None:
    order_id = unique("ord")
    seed(manifest(tmp_path, order(order_id, 5_000)), environ)
    refund = {
        "order_id": order_id,
        "amount_minor": 1_000,
        "currency": "USD",
        "action_id": unique("act"),
    }
    async with api_client() as client:
        response = await client.post(
            "/refunds",
            json=refund,
            headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": unique("key")},
        )
    assert response.status_code == 201

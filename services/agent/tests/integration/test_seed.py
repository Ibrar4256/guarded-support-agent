"""The agent's deploy-time seed (ADR-010 Part 2, ADR-019), against the real database as the
owner role."""

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import env
from sqlalchemy import Engine, delete, select, text

from agent.db.schema import customers, orders
from agent.seed import SeedError, main, seed


@pytest.fixture
def ids(owner_engine: Engine) -> Iterator[list[str]]:
    """Every order and customer id a test makes, deleted afterwards (owner role)."""
    made: list[str] = []
    yield made
    with owner_engine.begin() as connection:
        connection.execute(delete(orders).where(orders.c.id.in_(made)))
        connection.execute(delete(customers).where(customers.c.id.in_(made)))


@pytest.fixture
def environ() -> dict[str, str]:
    return {"AGENT_DB_OWNER_URL": env("AGENT_DB_OWNER_URL")}


def new_id(ids: list[str], prefix: str) -> str:
    made = f"{prefix}_{uuid.uuid4().hex[:10]}"
    ids.append(made)
    return made


def order(order_id: str, owner: str, **changes: Any) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "owner_user_id": owner,
        "charged_minor": 5_000,
        "currency": "USD",
        "status": "delivered",
        "delivered_days_ago": 3,
        "chargeback_open": False,
        **changes,
    }


def manifest(tmp_path: Path, *items: dict[str, Any]) -> Path:
    path = tmp_path / f"manifest_{uuid.uuid4().hex[:6]}.json"
    path.write_text(json.dumps({"manifest_version": 1, "orders": list(items)}), encoding="utf-8")
    return path


def row(engine: Engine, order_id: str) -> Any:
    with engine.connect() as connection:
        return connection.execute(select(orders).where(orders.c.id == order_id)).one_or_none()


def days_ago(engine: Engine, order_id: str) -> float:
    """How long ago ``delivered_at`` is, in days, on the database clock."""
    with engine.connect() as connection:
        return float(
            connection.execute(
                text(
                    "SELECT EXTRACT(EPOCH FROM now() - delivered_at) / 86400"
                    " FROM orders WHERE id = :o"
                ),
                {"o": order_id},
            ).scalar_one()
        )


def test_a_first_run_inserts_customers_and_orders_with_relative_dates(
    tmp_path: Path, environ: dict[str, str], owner_engine: Engine, ids: list[str]
) -> None:
    alice, delivered, shipped = new_id(ids, "user"), new_id(ids, "ord"), new_id(ids, "ord")
    report = seed(
        manifest(
            tmp_path,
            order(delivered, alice, delivered_days_ago=3),
            order(shipped, alice, status="shipped", delivered_days_ago=None, charged_minor=999),
        ),
        environ,
    )
    assert (report.inserted, report.refreshed, report.customers_inserted) == (2, 0, 1)
    assert abs(days_ago(owner_engine, delivered) - 3) < 0.01
    shipped_row = row(owner_engine, shipped)
    assert (shipped_row.status, shipped_row.delivered_at, shipped_row.charged_minor) == (
        "shipped",
        None,
        999,
    )


def test_a_re_run_updates_fulfillment_and_refreshes_dates(
    tmp_path: Path, environ: dict[str, str], owner_engine: Engine, ids: list[str]
) -> None:
    alice, made = new_id(ids, "user"), new_id(ids, "ord")
    seed(manifest(tmp_path, order(made, alice, status="shipped", delivered_days_ago=None)), environ)
    report = seed(
        manifest(tmp_path, order(made, alice, delivered_days_ago=1, chargeback_open=True)),
        environ,
    )
    assert (report.inserted, report.refreshed, report.customers_inserted) == (0, 1, 0)
    refreshed = row(owner_engine, made)
    assert (refreshed.status, refreshed.chargeback_open) == ("delivered", True)
    assert abs(days_ago(owner_engine, made) - 1) < 0.01


@pytest.mark.parametrize(
    "changed",
    [{"charged_minor": 6_000}, {"currency": "EUR"}, {"owner_user_id": "other"}],
    ids=["amount", "currency", "owner"],
)
def test_changed_facts_refuse_the_seed_and_write_nothing(
    tmp_path: Path,
    environ: dict[str, str],
    owner_engine: Engine,
    ids: list[str],
    changed: dict[str, Any],
) -> None:
    alice, made, fresh = new_id(ids, "user"), new_id(ids, "ord"), new_id(ids, "ord")
    if changed.get("owner_user_id"):
        changed = {"owner_user_id": new_id(ids, "user")}
    seed(manifest(tmp_path, order(made, alice, status="shipped", delivered_days_ago=None)), environ)
    with pytest.raises(SeedError, match=made):
        seed(manifest(tmp_path, order(made, alice, **changed), order(fresh, alice)), environ)
    assert row(owner_engine, fresh) is None, "not even the new order"
    assert row(owner_engine, made).status == "shipped", "nor the fulfillment update"


def test_main_reports_and_refuses(
    tmp_path: Path,
    environ: dict[str, str],
    ids: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AGENT_DB_OWNER_URL", environ["AGENT_DB_OWNER_URL"])
    alice, made = new_id(ids, "user"), new_id(ids, "ord")
    assert main([str(manifest(tmp_path, order(made, alice)))]) == 0
    assert "orders inserted: 1, refreshed: 0; customers inserted: 1" in capsys.readouterr().out
    assert main([str(manifest(tmp_path, order(made, alice, charged_minor=1)))]) == 1
    assert "seed refused, nothing written" in capsys.readouterr().err

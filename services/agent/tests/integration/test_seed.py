"""The agent's deploy-time seed (ADR-010 Part 2, ADR-019), against the real database as the
owner role."""

import json
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import env
from sqlalchemy import Engine, delete, select, text
from worker_world import World

from agent.core.lifecycle import RunStatus
from agent.db.schema import customers, orders
from agent.seed import SeedError, SeedNotEnabled, main, seed


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
    return {"AGENT_DB_OWNER_URL": env("AGENT_DB_OWNER_URL"), "DEMO_SEED_ENABLED": "true"}


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
    monkeypatch.setenv("DEMO_SEED_ENABLED", "true")
    alice, made = new_id(ids, "user"), new_id(ids, "ord")
    assert main([str(manifest(tmp_path, order(made, alice)))]) == 0
    assert "orders inserted: 1, refreshed: 0; customers inserted: 1" in capsys.readouterr().out
    assert main([str(manifest(tmp_path, order(made, alice, charged_minor=1)))]) == 1
    assert "seed refused, nothing written" in capsys.readouterr().err


# --- review additions (ADR-019 S5-S7) -------------------------------------------------------


def reseed_with_a_held_order(
    tmp_path: Path, environ: dict[str, str], ids: list[str], world: World, status: RunStatus
) -> tuple[str, str, Any]:
    """Two shipped orders; one gets a run in ``status``; a re-run marks both delivered with a
    chargeback."""
    alice, held, free = new_id(ids, "user"), new_id(ids, "ord"), new_id(ids, "ord")
    shipped = {"status": "shipped", "delivered_days_ago": None}
    seed(manifest(tmp_path, order(held, alice, **shipped), order(free, alice, **shipped)), environ)
    world.add_refund_run(held, 1_000, status)
    changed = {"delivered_days_ago": 2, "chargeback_open": True}
    report = seed(
        manifest(tmp_path, order(held, alice, **changed), order(free, alice, **changed)), environ
    )
    return held, free, report


def test_an_order_with_an_unfinished_run_is_left_alone_and_reported(
    tmp_path: Path, environ: dict[str, str], owner_engine: Engine, ids: list[str], world: World
) -> None:
    held, free, report = reseed_with_a_held_order(tmp_path, environ, ids, world, RunStatus.APPROVED)
    assert report.held == (held,) and report.refreshed == 1
    assert (row(owner_engine, held).status, row(owner_engine, held).chargeback_open) == (
        "shipped",
        False,
    ), "a deploy didn't change the facts under the waiting refund"
    assert row(owner_engine, free).chargeback_open is True


def test_a_finished_run_doesnt_hold_its_order(
    tmp_path: Path, environ: dict[str, str], owner_engine: Engine, ids: list[str], world: World
) -> None:
    held, _, report = reseed_with_a_held_order(tmp_path, environ, ids, world, RunStatus.ESCALATED)
    assert report.held == ()
    assert row(owner_engine, held).chargeback_open is True


def test_orders_the_manifest_doesnt_list_are_never_touched(
    tmp_path: Path, environ: dict[str, str], owner_engine: Engine, ids: list[str]
) -> None:
    alice, listed, unlisted = new_id(ids, "user"), new_id(ids, "ord"), new_id(ids, "ord")
    seed(
        manifest(tmp_path, order(listed, alice), order(unlisted, alice, chargeback_open=True)),
        environ,
    )
    before = row(owner_engine, unlisted)
    seed(manifest(tmp_path, order(listed, alice)), environ)
    assert row(owner_engine, unlisted) == before, "not refreshed, not deleted"


def test_without_the_demo_flag_nothing_is_written(
    tmp_path: Path,
    environ: dict[str, str],
    owner_engine: Engine,
    ids: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    made = new_id(ids, "ord")
    path = manifest(tmp_path, order(made, new_id(ids, "user")))
    for flag in (None, "", "false", "1", "TRUE"):
        with pytest.raises(SeedNotEnabled):
            seed(
                path,
                {k: v for k, v in environ.items() if k != "DEMO_SEED_ENABLED"}
                | ({} if flag is None else {"DEMO_SEED_ENABLED": flag}),
            )
    monkeypatch.setenv("AGENT_DB_OWNER_URL", environ["AGENT_DB_OWNER_URL"])
    monkeypatch.delenv("DEMO_SEED_ENABLED", raising=False)
    assert main([str(path)]) == 2
    assert "this isn't a demo deployment" in capsys.readouterr().err
    assert row(owner_engine, made) is None


def test_the_seed_waits_for_a_customer_lock_held_by_a_pre_send_check(
    tmp_path: Path, environ: dict[str, str], owner_engine: Engine, ids: list[str]
) -> None:
    """ADR-005's order: the seed takes the customers' locks first, so it can't change an order
    while a pre-send check for its customer holds the customer row."""
    alice, made = new_id(ids, "user"), new_id(ids, "ord")
    seed(manifest(tmp_path, order(made, alice)), environ)
    path = manifest(tmp_path, order(made, alice, chargeback_open=True))
    done = threading.Event()

    def reseed() -> None:
        seed(path, environ)
        done.set()

    worker = threading.Thread(target=reseed, daemon=True)
    with owner_engine.begin() as holder:
        holder.execute(
            select(customers.c.id).where(customers.c.id == alice).with_for_update()
        ).all()
        worker.start()
        assert not done.wait(1.0), "the seed waited for the customer lock"
    worker.join(timeout=10)
    assert done.is_set() and row(owner_engine, made).chargeback_open is True

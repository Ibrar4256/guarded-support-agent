"""Deploy-time seed for the agent (ADR-010 Part 2, ADR-019).

    python -m agent.seed <manifest.json>

Runs as the **owner** role (AGENT_DB_OWNER_URL), like migrations, and only with
``DEMO_SEED_ENABLED=true`` (it writes fake customers and orders: a real deployment's deploy step
must never run it by accident). In one transaction, taking the customers' locks first (ADR-005's
order):
- an existing order whose **facts** (owner, charged amount, currency) differ from the manifest
  refuses the whole seed, changing nothing: they mirror refund-api's immutable charge, which
  refund-api's own seed writes from the same manifest;
- otherwise missing customers and orders are inserted, and every listed order's **fulfillment
  state** (status, delivery time, chargeback flag) is set from the manifest, **except** for orders
  with an unfinished run: a deploy mustn't change the facts under a refund someone is waiting on,
  so those are left alone and reported;
- orders the manifest doesn't list are never touched or deleted (runs and decisions point at
  them);
- delivery times are relative (``delivered_days_ago``) and resolved on the database clock, so
  re-running the seed keeps demo orders inside ADR-009's refund window.

refund-api's seed reads only the charge fields of the same file.
"""

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import Connection, create_engine, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from agent import envvars
from agent.core.lifecycle import TERMINAL_STATUSES
from agent.core.policy import FulfillmentStatus
from agent.db.schema import customers, orders, runs
from agent.refund_client.models import BIGINT_MAX, CURRENCY_PATTERN, ID_PATTERN

OWNER_URL = "AGENT_DB_OWNER_URL"
ENABLED = "DEMO_SEED_ENABLED"


class SeedError(RuntimeError):
    """The seed was refused; nothing was written."""


class SeedNotEnabled(SeedError):
    """``DEMO_SEED_ENABLED`` isn't ``true``: not a demo deployment, nothing written."""


class ManifestOrder(BaseModel):
    # Strict, unknown fields refused (ADR-019 S1): a stale ``delivered_at`` must not be ignored.
    model_config = ConfigDict(extra="forbid", strict=True)

    order_id: str = Field(pattern=ID_PATTERN)
    owner_user_id: str = Field(pattern=ID_PATTERN)
    charged_minor: int = Field(ge=1, le=BIGINT_MAX)
    currency: str = Field(pattern=CURRENCY_PATTERN)
    status: FulfillmentStatus
    delivered_days_ago: int | None = Field(ge=0)
    chargeback_open: bool

    @model_validator(mode="after")
    def delivered_exactly_when_dated(self) -> Self:
        if (self.status is FulfillmentStatus.DELIVERED) != (self.delivered_days_ago is not None):
            raise ValueError("delivered_days_ago is set exactly when status is delivered")
        return self


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    manifest_version: Literal[1]
    orders: list[ManifestOrder] = Field(min_length=1)


@dataclass(frozen=True)
class SeedReport:
    inserted: int
    refreshed: int
    customers_inserted: int
    # Listed orders whose fulfillment state was left alone: they have an unfinished run.
    held: tuple[str, ...] = ()


def load_manifest(path: Path) -> Manifest:
    try:
        manifest = Manifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise SeedError(f"invalid manifest {path}: {exc}") from exc
    order_ids = [order.order_id for order in manifest.orders]
    duplicates = sorted({order_id for order_id in order_ids if order_ids.count(order_id) > 1})
    if duplicates:
        raise SeedError(f"duplicate order_id in manifest: {', '.join(duplicates)}")
    return manifest


def facts(order: ManifestOrder) -> tuple[str, int, str]:
    return (order.owner_user_id, order.charged_minor, order.currency)


def fulfillment(order: ManifestOrder) -> dict[str, Any]:
    delivered_at = (
        None
        if order.delivered_days_ago is None
        else func.now() - func.make_interval(0, 0, 0, order.delivered_days_ago)
    )
    return {
        "status": order.status,
        "delivered_at": delivered_at,
        "chargeback_open": order.chargeback_open,
    }


def apply(connection: Connection, wanted_orders: Sequence[ManifestOrder]) -> SeedReport:
    """In the caller's transaction: check every existing order's facts first, then write."""
    wanted = {order.order_id: order for order in wanted_orders}
    owners = sorted({order.owner_user_id for order in wanted_orders})
    # ADR-005's lock order: customers first. Holding them, no pre-send check for these customers
    # runs concurrently, so "has an unfinished run" can't change under the seed.
    connection.execute(
        select(customers.c.id)
        .where(customers.c.id.in_(owners))
        .order_by(customers.c.id)
        .with_for_update()
    ).all()
    existing = {
        row.id: (row.owner_user_id, row.charged_minor, row.currency)
        for row in connection.execute(
            select(orders).where(orders.c.id.in_(wanted)).with_for_update()
        )
    }
    mismatched = sorted(
        order_id for order_id, found in existing.items() if found != facts(wanted[order_id])
    )
    if mismatched:
        raise SeedError(
            "existing orders differ from the manifest in owner, amount or currency: "
            + ", ".join(mismatched)
        )
    held = tuple(
        sorted(
            connection.execute(
                select(runs.c.order_id)
                .where(
                    runs.c.order_id.in_(existing),
                    runs.c.status.not_in(sorted(TERMINAL_STATUSES)),
                )
                .distinct()
            ).scalars()
        )
    )
    added_customers = connection.execute(
        pg_insert(customers)
        .values([{"id": owner} for owner in owners])
        .on_conflict_do_nothing(index_elements=[customers.c.id])
        .returning(customers.c.id)
    ).all()
    new = [order for order_id, order in wanted.items() if order_id not in existing]
    if new:
        connection.execute(
            insert(orders).values(
                [
                    {
                        "id": order.order_id,
                        "owner_user_id": order.owner_user_id,
                        "charged_minor": order.charged_minor,
                        "currency": order.currency,
                        **fulfillment(order),
                    }
                    for order in new
                ]
            )
        )
    for order_id in existing:
        if order_id in held:
            continue
        connection.execute(
            update(orders).where(orders.c.id == order_id).values(**fulfillment(wanted[order_id]))
        )
    return SeedReport(
        inserted=len(new),
        refreshed=len(existing) - len(held),
        customers_inserted=len(added_customers),
        held=held,
    )


def seed(manifest_path: Path, environ: Mapping[str, str]) -> SeedReport:
    if environ.get(ENABLED) != "true":
        raise SeedNotEnabled(f"{ENABLED} is not 'true': this isn't a demo deployment")
    owner_url = envvars.required(environ, OWNER_URL, SeedError)
    manifest = load_manifest(manifest_path)
    engine = create_engine(owner_url)
    try:
        with engine.begin() as connection:
            return apply(connection, manifest.orders)
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m agent.seed <manifest.json>", file=sys.stderr)
        return 2
    try:
        report = seed(Path(args[0]), os.environ)
    except SeedNotEnabled as exc:
        print(f"seed not run, nothing written: {exc}", file=sys.stderr)
        return 2
    except SeedError as exc:
        print(f"seed refused, nothing written: {exc}", file=sys.stderr)
        return 1
    print(
        f"orders inserted: {report.inserted}, refreshed: {report.refreshed}; "
        f"customers inserted: {report.customers_inserted}"
    )
    if report.held:
        print(f"left alone (unfinished runs): {', '.join(report.held)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

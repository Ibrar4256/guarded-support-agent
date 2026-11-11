"""Deploy-time seed for refund-api (ADR-010 Part 2 and Part 5).

    python -m refund_api.seed <manifest.json>

Runs as the **owner** role (REFUND_DB_OWNER_URL), like migrations. It:
- inserts the charges the manifest lists that don't exist yet;
- refuses, changing nothing, if an existing charge differs from the manifest (charges
  are immutable, so a mismatch means the manifest or the database is wrong);
- sets the key-retention floor to REFUND_API_KEY_RETENTION_SECONDS, without which key
  pruning fails closed.

The manifest is shared with the agent's own seed command (orders and customers), which is
why it uses the agent's field names. This command reads only the charge fields; one input
keeps the two databases consistent (ADR-010 Part 2).
"""

import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Connection, create_engine, delete, insert, select

from refund_api.config import KEY_RETENTION, ConfigError, required, seconds
from refund_api.db.schema import charges, key_retention_policy
from refund_api.models import BIGINT_MAX, CURRENCY_PATTERN, ID_PATTERN

OWNER_URL = "REFUND_DB_OWNER_URL"
MANIFEST_VERSION = 1


class ManifestOrder(BaseModel):
    # The agent's seed reads the other fields (owner, status, delivery); refund-api ignores them.
    model_config = ConfigDict(extra="ignore", strict=True)

    order_id: str = Field(pattern=ID_PATTERN)
    charged_minor: int = Field(ge=1, le=BIGINT_MAX)
    currency: str = Field(pattern=CURRENCY_PATTERN)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    manifest_version: Literal[1]
    orders: list[ManifestOrder] = Field(min_length=1)


class SeedError(RuntimeError):
    """The seed was refused; nothing was written."""


@dataclass(frozen=True)
class SeedReport:
    inserted: int
    unchanged: int
    floor: timedelta


def load_manifest(path: Path) -> Manifest:
    try:
        manifest = Manifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise SeedError(f"invalid manifest {path}: {exc}") from exc
    order_ids = [order.order_id for order in manifest.orders]
    duplicates = sorted({order_id for order_id in order_ids if order_ids.count(order_id) > 1})
    if duplicates:
        raise SeedError(f"duplicate order_id in manifest: {', '.join(duplicates)}")
    return manifest


def apply(connection: Connection, orders: Sequence[ManifestOrder], floor: timedelta) -> SeedReport:
    """In the caller's transaction: check every charge first, then write."""
    wanted = {order.order_id: order for order in orders}
    existing = {
        row.order_id: row
        for row in connection.execute(select(charges).where(charges.c.order_id.in_(wanted)))
    }
    mismatched = sorted(
        order_id
        for order_id, row in existing.items()
        if (row.amount_minor, row.currency)
        != (wanted[order_id].charged_minor, wanted[order_id].currency)
    )
    if mismatched:
        raise SeedError(f"existing charges differ from the manifest: {', '.join(mismatched)}")
    new = [order for order_id, order in wanted.items() if order_id not in existing]
    if new:
        connection.execute(
            insert(charges),
            [
                {"order_id": o.order_id, "amount_minor": o.charged_minor, "currency": o.currency}
                for o in new
            ],
        )
    connection.execute(delete(key_retention_policy))
    connection.execute(insert(key_retention_policy).values(id=1, min_retention=floor))
    return SeedReport(inserted=len(new), unchanged=len(existing), floor=floor)


def seed(manifest_path: Path, environ: Mapping[str, str]) -> SeedReport:
    floor = seconds(environ, KEY_RETENTION)
    if floor <= timedelta(0):
        raise ConfigError(f"{KEY_RETENTION} must be positive")
    manifest = load_manifest(manifest_path)
    engine = create_engine(required(environ, OWNER_URL))
    try:
        with engine.begin() as connection:
            return apply(connection, manifest.orders, floor)
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m refund_api.seed <manifest.json>", file=sys.stderr)
        return 2
    try:
        report = seed(Path(args[0]), os.environ)
    except (SeedError, ConfigError) as exc:
        print(f"seed refused, nothing written: {exc}", file=sys.stderr)
        return 1
    print(
        f"charges inserted: {report.inserted}, already present: {report.unchanged}; "
        f"key retention floor: {report.floor.total_seconds():g}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

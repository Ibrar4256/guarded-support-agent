"""Deploy-time key-retention floor for refund-api (ADR-010 Part 5, ADR-021).

    python -m refund_api.retention_floor

Runs as the **owner** role (REFUND_DB_OWNER_URL), like migrations, on **every** deployment. It
sets the floor below which no idempotency key is ever pruned to
REFUND_API_KEY_RETENTION_SECONDS; without it, key pruning fails closed and every refund fails.
A re-run replaces the floor (lowering it too: that is the owner's decision).

It is the only writer of the floor (ADR-021 S1). Demo charges are a separate, demo-only command
(``refund_api.seed``), so a real deployment sets the floor without any demo data.
"""

import os
import sys
from collections.abc import Mapping, Sequence
from datetime import timedelta

from sqlalchemy import Connection, create_engine, delete, insert

from refund_api.config import KEY_RETENTION, ConfigError, required, seconds
from refund_api.db.schema import key_retention_policy

OWNER_URL = "REFUND_DB_OWNER_URL"


def retention(environ: Mapping[str, str]) -> timedelta:
    floor = seconds(environ, KEY_RETENTION)
    if floor <= timedelta(0):
        raise ConfigError(f"{KEY_RETENTION} must be positive")
    return floor


def apply(connection: Connection, floor: timedelta) -> None:
    """In the caller's transaction: replace the one-row floor."""
    connection.execute(delete(key_retention_policy))
    connection.execute(insert(key_retention_policy).values(id=1, min_retention=floor))


def set_floor(environ: Mapping[str, str]) -> timedelta:
    floor = retention(environ)
    engine = create_engine(required(environ, OWNER_URL))
    try:
        with engine.begin() as connection:
            apply(connection, floor)
    finally:
        engine.dispose()
    return floor


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        print("usage: python -m refund_api.retention_floor", file=sys.stderr)
        return 2
    try:
        floor = set_floor(os.environ)
    except ConfigError as exc:
        print(f"floor not set, nothing written: {exc}", file=sys.stderr)
        return 1
    print(f"key retention floor: {floor.total_seconds():g}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

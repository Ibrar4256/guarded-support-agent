"""Alembic environment for the agent database. Migrations run as the owner role."""

import os

from alembic import context
from sqlalchemy import create_engine, pool

from agent.db.schema import metadata

OWNER_URL_VARIABLE = "AGENT_DB_OWNER_URL"


def owner_url() -> str:
    url = os.environ.get(OWNER_URL_VARIABLE)
    if not url:
        raise RuntimeError(f"{OWNER_URL_VARIABLE} is not set; migrations run as the owner role")
    return url


def run_migrations() -> None:
    engine = create_engine(owner_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported; run against a database")
run_migrations()

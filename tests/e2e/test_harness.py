"""The harness itself (ADR-015): both services start with the one timing profile, and a
test's database is set up exactly like the real one. A failure here is a harness problem,
never a crash-test result."""

from profile import CLIENT_TIMEOUT_SECONDS, REFUND_API
from typing import Any

from conftest import E2E
from harness import Postgres

PRIVILEGES = """
SELECT c.relname, p.privilege, has_table_privilege('agent_runtime', c.oid, p.privilege)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) AS p(privilege)
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY 1, 2
"""
CONNECT = """
SELECT acl.grantee = 0 AS public, has_database_privilege('agent_runtime', d.oid, 'CONNECT')
FROM pg_database d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) acl
WHERE d.datname = current_database() AND acl.privilege_type = 'CONNECT'
ORDER BY 1
"""


async def grants(postgres: Postgres, database: str) -> tuple[list[Any], list[Any]]:
    tables = await postgres.fetch(database, PRIVILEGES)
    connect = sorted(set(await postgres.fetch(database, CONNECT)))
    return tables, connect


async def test_refund_api_runs_with_the_profile(e2e: E2E) -> None:
    async with e2e.refund_api.client() as http:
        config = (await http.get("/config")).json()
    assert (
        config["max_retry_window_ms"]
        == int(REFUND_API["REFUND_API_MAX_RETRY_WINDOW_SECONDS"]) * 1000
    )
    assert config["key_retention_ms"] == int(REFUND_API["REFUND_API_KEY_RETENTION_SECONDS"]) * 1000
    assert config["max_retry_window_ms"] > CLIENT_TIMEOUT_SECONDS * 1000


async def test_the_worker_starts_with_the_profile_and_stops_cleanly(e2e: E2E) -> None:
    worker = await e2e.start_worker()
    await worker.wait_for_output("claim gate open", seconds=30)
    assert await worker.terminate() == 0


async def test_a_test_database_has_the_real_databases_grants(e2e: E2E) -> None:
    real = await grants(e2e.postgres, "agent")
    assert real[0], "the real agent database is migrated"
    assert await grants(e2e.postgres, e2e.database) == real


async def test_negative_control_without_the_reapplied_grants_anyone_may_connect(e2e: E2E) -> None:
    """CREATE DATABASE ... TEMPLATE doesn't copy database-level grants."""
    bare = await e2e.postgres.create_test_database(grants=False)
    _, connect = await grants(e2e.postgres, bare)
    _, real = await grants(e2e.postgres, "agent")
    assert connect != real and (True, True) in connect, "PUBLIC can connect to the bare copy"

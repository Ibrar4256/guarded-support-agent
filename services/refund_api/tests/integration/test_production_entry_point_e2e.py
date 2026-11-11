"""A refund through the real production entry point (``app_from_env``), not only through
``create_app``: settings loading and wiring are part of what ships (ADR-011)."""

from typing import Any

import pytest
from conftest import create_charge, env, refund_rows, unique
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine

from refund_api import config
from refund_api.app import app_from_env

pytestmark = pytest.mark.usefixtures("retention_floor")
TOKEN = "e" * config.MIN_TOKEN_LENGTH
AMOUNT_FOR_ENTRY_POINT = 1_000


@pytest.fixture
def production_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in config.TEST_ONLY_FLAGS:
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        config.DATABASE_URL: env("REFUND_DB_RUNTIME_URL"),
        config.TOKEN_CURRENT: TOKEN,
        config.PROCESSING_BUDGET: "5",
        config.KEY_RETENTION: "86400",
        config.MAX_RETRY_WINDOW: "600",
        config.MAX_RECOVERY_DELAY: "3600",
        config.POOL_TIMEOUT: "2",
        config.APP_ENV: "production",
    }.items():
        monkeypatch.setenv(name, value)
    return app_from_env()


async def test_a_refund_through_the_production_entry_point(
    production_app: Any, runtime_engine: Engine
) -> None:
    order, action = create_charge(runtime_engine, AMOUNT_FOR_ENTRY_POINT), unique("act")
    body = {
        "order_id": order,
        "amount_minor": AMOUNT_FOR_ENTRY_POINT,
        "currency": "USD",
        "action_id": action,
    }
    transport = ASGITransport(app=production_app)
    try:
        async with AsyncClient(transport=transport, base_url="http://refund-api") as client:
            anonymous = await client.post("/refunds", json=body, headers={"Idempotency-Key": "k"})
            created = await client.post(
                "/refunds",
                json=body,
                headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": unique("key")},
            )
            test_route = await client.post(
                "/_test/reset", headers={"Authorization": f"Bearer {TOKEN}"}
            )
    finally:
        await production_app.state.engine.dispose()
    assert anonymous.status_code == 401
    assert created.status_code == 201
    assert test_route.status_code == 404
    assert refund_rows(runtime_engine, action_id=action) == 1

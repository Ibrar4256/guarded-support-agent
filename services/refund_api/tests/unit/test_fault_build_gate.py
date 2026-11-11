"""The fault-injection build starts only when asked, never in production (ADR-011)."""

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from refund_api_testing.app import create_fault_app

from refund_api.app import create_app
from refund_api.config import MIN_TOKEN_LENGTH, ConfigError, Settings

TOKEN = "t" * MIN_TOKEN_LENGTH
SETTINGS = Settings(
    database_url="postgresql+psycopg://nobody:none@127.0.0.1:1/none",
    tokens=(TOKEN,),
    processing_budget=timedelta(seconds=5),
    key_retention=timedelta(hours=24),
    max_retry_window=timedelta(minutes=10),
    max_recovery_delay=timedelta(hours=1),
    pool_timeout=timedelta(seconds=2),
    app_env="test",
)


@pytest.mark.parametrize(
    "environ",
    [
        {"APP_ENV": "test"},
        {"APP_ENV": "test", "DEBUG_FAULT_INJECTION": "1"},
        {"APP_ENV": "production", "DEBUG_FAULT_INJECTION": "true"},
        {"APP_ENV": "production"},
    ],
    ids=["flag-unset", "flag-not-true", "production-with-flag", "production"],
)
def test_fault_build_refuses_to_start(environ: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        create_fault_app(SETTINGS, environ)


async def test_control_fault_build_starts_with_the_flag() -> None:
    app = create_fault_app(SETTINGS, {"APP_ENV": "test", "DEBUG_FAULT_INJECTION": "true"})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as client:
        response = await client.post("/_test/reset", headers={"Authorization": f"Bearer {TOKEN}"})
    await app.state.engine.dispose()
    assert response.status_code == 204


async def test_production_app_has_no_test_routes() -> None:
    app = create_app(SETTINGS)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://x") as client:
        response = await client.post("/_test/reset", headers={"Authorization": f"Bearer {TOKEN}"})
    await app.state.engine.dispose()
    assert response.status_code == 404


def test_test_routes_are_not_in_the_contract() -> None:
    app = create_fault_app(SETTINGS, {"APP_ENV": "test", "DEBUG_FAULT_INJECTION": "true"})
    assert not any(path.startswith("/_test") for path in app.openapi()["paths"])

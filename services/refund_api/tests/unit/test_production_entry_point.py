"""The production entry point (ADR-011): settings from the environment, middleware in
ADR-010's order, and only the no-op hooks. ``deduplicates`` can switch off idempotency, so
nothing may install other hooks in production."""

from typing import Any

import pytest
from refund_api_testing.app import create_fault_app

from refund_api import config
from refund_api.app import AuthMiddleware, ReceiptMiddleware, app_from_env, create_app
from refund_api.hooks import RefundHooks

TOKEN = "p" * config.MIN_TOKEN_LENGTH
ENVIRON = {
    config.DATABASE_URL: "postgresql+psycopg://nobody:none@127.0.0.1:1/none",
    config.TOKEN_CURRENT: TOKEN,
    config.PROCESSING_BUDGET: "5",
    config.KEY_RETENTION: "86400",
    config.MAX_RETRY_WINDOW: "600",
    config.MAX_RECOVERY_DELAY: "3600",
    config.POOL_TIMEOUT: "2",
    config.APP_ENV: "production",
}


@pytest.fixture
def production_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in config.TEST_ONLY_FLAGS:
        monkeypatch.delenv(name, raising=False)
    for name, value in ENVIRON.items():
        monkeypatch.setenv(name, value)
    return app_from_env()


def runs_only_no_op_hooks(app: Any) -> bool:
    return type(app.state.hooks) is RefundHooks


def test_production_entry_point_runs_only_no_op_hooks(production_app: Any) -> None:
    assert runs_only_no_op_hooks(production_app)


def test_control_the_fault_build_fails_the_same_check() -> None:
    settings = config.Settings.from_env({**ENVIRON, config.APP_ENV: "test"})
    fault_app = create_fault_app(settings, {"APP_ENV": "test", "DEBUG_FAULT_INJECTION": "true"})
    assert not runs_only_no_op_hooks(fault_app)


def test_control_any_other_hooks_fail_the_check() -> None:
    class Sneaky(RefundHooks):
        def deduplicates(self, context: Any) -> bool:
            return False

    settings = config.Settings.from_env({**ENVIRON, config.APP_ENV: "test"})
    assert not runs_only_no_op_hooks(create_app(settings, Sneaky()))


def test_receipt_middleware_wraps_auth(production_app: Any) -> None:
    # Starlette's user_middleware is outermost first (ADR-010: receipt, then auth).
    order = [entry.cls for entry in production_app.user_middleware]
    assert order == [ReceiptMiddleware, AuthMiddleware]


def test_production_entry_point_reads_settings_from_the_environment(production_app: Any) -> None:
    assert production_app.state.engine.url.database == "none"

"""The refund-api HTTP app (ADR-004, ADR-010).

Middleware order matters. Starlette runs the middleware added **last** first, so:
1. ``ReceiptMiddleware`` (outermost) records receipt on the monotonic clock, so time spent
   authenticating counts against the processing budget (ADR-010).
2. ``AuthMiddleware`` checks the bearer token before routing or validation, so an
   unauthenticated request gets 401, never a 400 that reveals the schema (Part 6).

At startup the app refuses to serve unless the key-retention floor is set (ADR-021 Part 2), so a
deploy that skipped ``python -m refund_api.retention_floor`` stops here, not at the first refund.

Run it with ``uvicorn --factory refund_api.app:app_from_env``.
"""

import asyncio
import hmac
import os
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.types import ASGIApp, Receive, Scope, Send

from refund_api.config import Settings
from refund_api.hooks import RefundHooks
from refund_api.models import (
    ID_PATTERN,
    KEY_PATTERN,
    BusinessError,
    BusinessErrorCode,
    InFlight,
    KeyMismatch,
    ProviderConfig,
    Refund,
    RefundList,
    RefundRequest,
    Unauthorized,
    Unavailable,
)
from refund_api.service import create_refund, list_refunds

JSON = "application/json"
CONTRACT_MODELS = (
    RefundRequest,
    Refund,
    RefundList,
    BusinessError,
    InFlight,
    KeyMismatch,
    ProviderConfig,
    Unavailable,
    Unauthorized,
)
BEARER_SCHEME = {"bearerAuth": {"type": "http", "scheme": "bearer"}}
NUMERIC_BOUNDS = frozenset({"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"})
RECEIVED_AT = "received_at"
UNAUTHENTICATED_PATHS = frozenset({"/health"})
AUTH_FAILED = Unauthorized().model_dump_json().encode()


class ReceiptMiddleware:
    """Stamps request receipt on the monotonic clock: the start of the processing budget."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope.setdefault("state", {})[RECEIVED_AT] = time.monotonic()
        await self.app(scope, receive, send)


class AuthMiddleware:
    """Bearer service token; any of the configured tokens is accepted (rotation)."""

    def __init__(self, app: ASGIApp, tokens: Sequence[str]) -> None:
        self.app = app
        self.expected = [f"Bearer {token}".encode() for token in tokens]

    def authorized(self, header: bytes) -> bool:
        # Compare against every token, without stopping early.
        matches = [hmac.compare_digest(header, expected) for expected in self.expected]
        return any(matches)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in UNAUTHENTICATED_PATHS:
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        if self.authorized(headers.get(b"authorization", b"")):
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", JSON.encode()), (b"www-authenticate", b"Bearer")],
            }
        )
        await send({"type": "http.response.body", "body": AUTH_FAILED})


def invalid_request(_: Request, exc: Exception) -> Response:
    """Every validation failure (body, header, query, bad JSON) → 400 invalid_request."""
    if not isinstance(exc, RequestValidationError):
        raise exc
    fields = [".".join(str(part) for part in error["loc"]) for error in exc.errors()]
    body = BusinessError(code=BusinessErrorCode.INVALID_REQUEST, fields=fields)
    return Response(body.model_dump_json(), status_code=400, media_type=JSON)


def response_spec(model: type[Any], description: str) -> dict[str, Any]:
    return {"model": model, "description": description}


FLOOR_CHECK = text("SELECT public.retention_floor_is_set()")
FLOOR_MISSING = (
    "key retention floor is not set, refund-api not started: run "
    "`python -m refund_api.retention_floor` as the owner role (REFUND_DB_OWNER_URL) before "
    "starting refund-api (ADR-021)"
)


class StartupError(RuntimeError):
    """refund-api must not serve: the deploy is incomplete or the database is unreachable."""


async def check_retention_floor(engine: AsyncEngine, timeout: timedelta) -> None:
    """ADR-021 Part 2 and S8: the floor must be set, and an unreachable database (or one not
    migrated to head) stops the start too, under an explicit timeout because the pool timeout
    doesn't bound a connect."""
    try:
        async with asyncio.timeout(timeout.total_seconds()):
            async with engine.connect() as connection:
                floor_is_set = (await connection.execute(FLOOR_CHECK)).scalar_one()
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        raise StartupError(
            "startup check failed, refund-api not started (database unreachable or not "
            f"migrated to head): {exc!r}"
        ) from exc
    if not floor_is_set:
        raise StartupError(FLOOR_MISSING)


def create_app(settings: Settings, hooks: RefundHooks | None = None) -> FastAPI:
    engine: AsyncEngine = create_async_engine(
        settings.database_url, pool_timeout=settings.pool_timeout.total_seconds()
    )
    refund_hooks = hooks or RefundHooks()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await check_retention_floor(engine, settings.pool_timeout)
        yield

    # No interactive docs or public spec: the service isn't public (ADR-010 Part 6).
    # The spec is exported from app.openapi() into contracts/.
    app = FastAPI(
        title="refund-api", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.engine = engine
    # Kept so a test can assert the production entry point runs the no-op hooks only:
    # `deduplicates` can switch off idempotency (ADR-011).
    app.state.hooks = refund_hooks
    app.add_exception_handler(RequestValidationError, invalid_request)

    common: dict[int | str, dict[str, Any]] = {
        400: response_spec(BusinessError, "Business error or invalid request; nothing stored"),
        401: response_spec(Unauthorized, "Missing or wrong service token; nothing done"),
        503: response_spec(Unavailable, "Deadline exceeded or unavailable; nothing committed"),
    }

    @app.post(
        "/refunds",
        status_code=201,
        response_model=None,
        responses={
            201: response_spec(Refund, "Created, or replayed with Idempotent-Replayed: true"),
            **common,
            409: response_spec(InFlight, "The same key is being processed; retry later"),
            422: response_spec(KeyMismatch, "The key was used with a different payload"),
        },
    )
    async def post_refund(
        request: Request,
        refund: RefundRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", pattern=KEY_PATTERN)],
    ) -> Response:
        outcome = await create_refund(
            engine,
            settings,
            refund_hooks,
            refund,
            idempotency_key,
            getattr(request.state, RECEIVED_AT),
        )
        return Response(
            outcome.body, status_code=outcome.status, media_type=JSON, headers=outcome.headers
        )

    @app.get(
        "/refunds",
        response_model=None,
        responses={200: response_spec(RefundList, "Every refund for this action"), **common},
    )
    async def get_refunds(
        order_id: Annotated[str, Query(pattern=ID_PATTERN)],
        action_id: Annotated[str, Query(pattern=ID_PATTERN)],
    ) -> Response:
        return Response(await list_refunds(engine, order_id, action_id), media_type=JSON)

    provider_config = ProviderConfig(
        max_retry_window_ms=milliseconds(settings.max_retry_window),
        key_retention_ms=milliseconds(settings.key_retention),
        processing_budget_ms=milliseconds(settings.processing_budget),
    ).model_dump_json()

    @app.get(
        "/config",
        response_model=None,
        responses={
            200: response_spec(ProviderConfig, "The provider's retry window and key lifetime"),
            401: common[401],
        },
    )
    async def get_config() -> Response:
        # Behind the same bearer auth: it doubles as the agent's token probe (ADR-012 Part 4).
        return Response(provider_config, media_type=JSON)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = with_bearer_auth(
                with_exact_bounds(
                    without_framework_422(
                        get_openapi(title=app.title, version=app.version, routes=app.routes)
                    )
                )
            )
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]

    app.add_middleware(AuthMiddleware, tokens=settings.tokens)
    app.add_middleware(ReceiptMiddleware)  # added last = outermost
    return app


def without_framework_422(spec: dict[str, Any]) -> dict[str, Any]:
    """Drop FastAPI's auto-added 422 validation responses; ours is the key mismatch only."""
    framework_ref = "#/components/schemas/HTTPValidationError"
    for operations in spec.get("paths", {}).values():
        for operation in operations.values():
            response = operation.get("responses", {}).get("422")
            content = (response or {}).get("content", {}).get(JSON, {})
            if content.get("schema", {}).get("$ref") == framework_ref:
                del operation["responses"]["422"]
    schemas = spec.get("components", {}).get("schemas", {})
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)
    return spec


def with_exact_bounds(spec: dict[str, Any]) -> dict[str, Any]:
    """Put back the exact numeric bounds FastAPI's spec model rounds to float.

    FastAPI types ``minimum``/``maximum`` as float, so ``le=2**63 - 1`` comes out as
    9.223372036854776e+18, which is 2**63: one more than a BIGINT holds, and a client
    validating against the contract would accept an amount the service rejects. The models'
    own Pydantic schemas keep the exact integers; copy them back.
    """
    schemas = spec.get("components", {}).get("schemas", {})
    for model in CONTRACT_MODELS:
        if model.__name__ in schemas:
            exact = model.model_json_schema(ref_template="#/components/schemas/{model}")
            _restore_bounds(schemas[model.__name__], exact)
    return spec


def _restore_bounds(target: Any, source: Any) -> None:
    if isinstance(target, dict) and isinstance(source, dict):
        for key, value in source.items():
            if key in NUMERIC_BOUNDS and type(value) is int and isinstance(target.get(key), float):
                target[key] = value
            elif key in target:
                _restore_bounds(target[key], value)
    elif isinstance(target, list) and isinstance(source, list):
        for target_item, source_item in zip(target, source, strict=False):
            _restore_bounds(target_item, source_item)


def with_bearer_auth(spec: dict[str, Any]) -> dict[str, Any]:
    """Declare the bearer auth the middleware enforces, so the contract says every route
    needs a token (``/health`` isn't in the spec)."""
    spec.setdefault("components", {})["securitySchemes"] = BEARER_SCHEME
    spec["security"] = [{name: []} for name in BEARER_SCHEME]
    return spec


def milliseconds(duration: timedelta) -> int:
    return duration // timedelta(milliseconds=1)


def app_from_env() -> FastAPI:
    return create_app(Settings.from_env(os.environ))

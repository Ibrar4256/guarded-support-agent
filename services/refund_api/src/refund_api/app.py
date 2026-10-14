"""The refund-api HTTP app (ADR-004, ADR-010).

Middleware order matters. Starlette runs the middleware added **last** first, so:
1. ``ReceiptMiddleware`` (outermost) records receipt on the monotonic clock, so time spent
   authenticating counts against the processing budget (ADR-010).
2. ``AuthMiddleware`` checks the bearer token before routing or validation, so an
   unauthenticated request gets 401, never a 400 that reveals the schema (Part 6).

Run it with ``uvicorn --factory refund_api.app:app_from_env``.
"""

import hmac
import os
import time
from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import Response
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
    Refund,
    RefundList,
    RefundRequest,
    Unauthorized,
    Unavailable,
)
from refund_api.service import create_refund, list_refunds

JSON = "application/json"
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


def create_app(settings: Settings, hooks: RefundHooks | None = None) -> FastAPI:
    engine: AsyncEngine = create_async_engine(
        settings.database_url, pool_timeout=settings.pool_timeout.total_seconds()
    )
    refund_hooks = hooks or RefundHooks()
    # No interactive docs or public spec: the service isn't public (ADR-010 Part 6).
    # The spec is exported from app.openapi() into contracts/.
    app = FastAPI(title="refund-api", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
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

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = without_framework_422(
                get_openapi(title=app.title, version=app.version, routes=app.routes)
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


def app_from_env() -> FastAPI:
    return create_app(Settings.from_env(os.environ))

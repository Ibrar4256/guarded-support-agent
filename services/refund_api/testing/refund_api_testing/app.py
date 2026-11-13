"""The fault-injection build of refund-api (ADR-011).

The test package wraps the production app, never the other way round: it builds the normal
app with ``FaultHooks`` and mounts test-only routes under ``/_test``. Production code never
imports this package, and a production image doesn't contain it.

Run it with ``uvicorn --factory refund_api_testing.app:fault_app_from_env --workers 1``.
Plans live in this process's memory, so it must run as a single worker (ADR-010 Part 3);
every fault test asserts its plan was applied, which catches a multi-worker mistake.
"""

import os
from collections.abc import Mapping
from datetime import timedelta
from typing import Literal
from urllib.parse import parse_qs

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Receive, Scope, Send

from refund_api.app import JSON, create_app
from refund_api.config import APP_ENV, PRODUCTION, ConfigError, Settings, check_production_gate
from refund_api.db.schema import charges
from refund_api.models import CURRENCY_PATTERN, ID_PATTERN, BusinessError, BusinessErrorCode
from refund_api_testing.faults import (
    FaultBook,
    FaultHooks,
    FaultMode,
    FaultPlan,
    InjectedFault,
    InjectedInvalidRequest,
)

FAULT_FLAG = "DEBUG_FAULT_INJECTION"
TEST_PREFIX = "/_test"


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(pattern=ID_PATTERN)
    mode: FaultMode
    seconds: float | None = Field(default=None, gt=0)
    key_retention_seconds: float | None = Field(default=None, gt=0)


class PlanState(BaseModel):
    mode: FaultMode | None
    used: bool
    applied: list[str]


class ChargeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    order_id: str = Field(pattern=ID_PATTERN)
    amount_minor: int = Field(ge=1)
    currency: str = Field(pattern=CURRENCY_PATTERN)


class Injected(BaseModel):
    status: Literal["injected_fault"] = "injected_fault"


def check_test_build_allowed(environ: Mapping[str, str]) -> None:
    """The test build starts only with the flag on, and never in production."""
    check_production_gate(environ)
    if environ.get(APP_ENV) == PRODUCTION:
        raise ConfigError("the fault-injection build never runs in production")
    if environ.get(FAULT_FLAG) != "true":
        raise ConfigError(f"the fault-injection build needs {FAULT_FLAG}=true")


def injected_fault(_: Request, exc: Exception) -> Response:
    if not isinstance(exc, InjectedFault):
        raise exc
    return Response(Injected().model_dump_json(), status_code=500, media_type=JSON)


def injected_invalid_request(_: Request, exc: Exception) -> Response:
    """The same body refund-api's validation handler sends, with no field locations."""
    if not isinstance(exc, InjectedInvalidRequest):
        raise exc
    body = BusinessError(code=BusinessErrorCode.INVALID_REQUEST)
    return Response(body.model_dump_json(), status_code=400, media_type=JSON)


class FailingLookups:
    """``fail_all`` for lookups: ``GET /refunds`` for a planned action answers 500. Production has
    no hook in the lookup path, and gets none: this runs innermost, after auth and the receipt
    middleware, so an unauthenticated request is still refused first."""

    def __init__(self, app: ASGIApp, book: FaultBook) -> None:
        self.app, self.book = app, book

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"] == "/refunds":
            query = parse_qs(scope.get("query_string", b"").decode())
            action_id = (query.get("action_id") or [""])[0]
            if action_id and self.book.fire(action_id, FaultMode.FAIL_ALL):
                failed = Response(Injected().model_dump_json(), status_code=500, media_type=JSON)
                await failed(scope, receive, send)
                return
        await self.app(scope, receive, send)


def fault_routes(app: FastAPI, book: FaultBook) -> APIRouter:
    router = APIRouter(prefix=TEST_PREFIX, include_in_schema=False)

    @router.post("/faults", status_code=201)
    async def register(plan: PlanRequest) -> PlanState:
        retention = (
            timedelta(seconds=plan.key_retention_seconds) if plan.key_retention_seconds else None
        )
        try:
            fault = FaultPlan(mode=plan.mode, seconds=plan.seconds, key_retention=retention)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        book.register(plan.action_id, fault)
        return state(book, plan.action_id)

    @router.get("/faults/{action_id}")
    async def read(action_id: str) -> PlanState:
        return state(book, action_id)

    @router.delete("/faults/{action_id}", status_code=204)
    async def remove(action_id: str) -> None:
        book.remove(action_id)

    @router.post("/reset", status_code=204)
    async def reset() -> None:
        # Between test sessions only: a fixture removes just its own actions.
        book.reset()

    @router.post("/charges", status_code=201)
    async def create_charge(charge: ChargeRequest) -> ChargeRequest:
        try:
            async with app.state.engine.begin() as connection:
                await connection.execute(insert(charges).values(**charge.model_dump()))
        except IntegrityError:
            detail = f"charge for {charge.order_id} already exists"
            raise HTTPException(status_code=409, detail=detail) from None
        return charge

    return router


def state(book: FaultBook, action_id: str) -> PlanState:
    plan = book.plans.get(action_id)
    return PlanState(
        mode=plan.mode if plan else None,
        used=plan.used if plan else False,
        applied=list(book.applied.get(action_id, [])),
    )


def create_fault_app(
    settings: Settings, environ: Mapping[str, str], book: FaultBook | None = None
) -> FastAPI:
    check_test_build_allowed(environ)
    faults = book or FaultBook()
    app = create_app(settings, FaultHooks(faults))
    app.state.faults = faults
    app.add_exception_handler(InjectedFault, injected_fault)
    app.add_exception_handler(InjectedInvalidRequest, injected_invalid_request)
    # Innermost (add_middleware would make it outermost, before auth).
    app.user_middleware.append(Middleware(FailingLookups, book=faults))
    app.include_router(fault_routes(app, faults))
    return app


def fault_app_from_env() -> FastAPI:
    return create_fault_app(Settings.from_env(os.environ), os.environ)

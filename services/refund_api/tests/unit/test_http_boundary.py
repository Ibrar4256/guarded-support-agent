"""Auth, validation and the OpenAPI shape (ADR-010 Parts 1 and 6). No database needed:
every request here is refused before the handler would open a connection."""

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from httpx import ASGITransport, AsyncClient

from refund_api.app import create_app
from refund_api.config import MIN_TOKEN_LENGTH, Settings
from refund_api.models import BusinessErrorCode

TOKEN = "t" * MIN_TOKEN_LENGTH
AUTH = {"Authorization": f"Bearer {TOKEN}"}
VALID = {"order_id": "ord_1", "amount_minor": 1000, "currency": "USD", "action_id": "act_1"}


def make_app() -> Any:
    return create_app(
        Settings(
            database_url="postgresql+psycopg://nobody:none@127.0.0.1:1/none",
            tokens=(TOKEN,),
            processing_budget=timedelta(seconds=5),
            key_retention=timedelta(hours=24),
            max_retry_window=timedelta(minutes=10),
            max_recovery_delay=timedelta(hours=1),
            pool_timeout=timedelta(seconds=2),
            app_env="test",
        )
    )


async def client_for(app: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://refund-api") as c:
        yield c
    await app.state.engine.dispose()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async for c in client_for(make_app()):
        yield c


# --- authentication -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer wrong-token-of-sufficient-length-xxxxxxxx", f"Basic {TOKEN}", TOKEN],
    ids=["missing", "wrong", "wrong-scheme", "no-scheme"],
)
async def test_unauthenticated_post_and_get_are_401(
    client: AsyncClient, authorization: str | None
) -> None:
    headers = {"Idempotency-Key": "key_1"} | (
        {"Authorization": authorization} if authorization else {}
    )
    post = await client.post("/refunds", json=VALID, headers=headers)
    get = await client.get("/refunds", params={"order_id": "o", "action_id": "a"}, headers=headers)
    for response in (post, get):
        assert response.status_code == 401
        assert response.json() == {"status": "unauthorized"}
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("authorization", ["Bearer ", "Bearer", ""], ids=["empty", "bare", "blank"])
async def test_an_empty_bearer_token_is_never_accepted(
    client: AsyncClient, authorization: str
) -> None:
    # With REFUND_API_TOKEN_NEXT empty, there must be no "empty" token that matches.
    response = await client.get(
        "/refunds",
        params={"order_id": "o", "action_id": "a"},
        headers={"Authorization": authorization},
    )
    assert response.status_code == 401


async def test_auth_runs_before_validation(client: AsyncClient) -> None:
    # A malformed body without a token gets 401, never a 400 that reveals the schema.
    response = await client.post("/refunds", content=b"{not json", headers={"Idempotency-Key": "k"})
    assert response.status_code == 401


async def test_health_needs_no_token(client: AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}


async def test_no_public_docs_or_spec(client: AsyncClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path, headers=AUTH)).status_code == 404


# --- validation → 400 invalid_request -----------------------------------------------------

INVALID_BODIES = {
    "amount-as-string": VALID | {"amount_minor": "1000"},
    "amount-as-float": VALID | {"amount_minor": 1000.0},
    "amount-as-bool": VALID | {"amount_minor": True},
    "amount-zero": VALID | {"amount_minor": 0},
    "amount-above-bigint": VALID | {"amount_minor": 2**63},
    "lowercase-currency": VALID | {"currency": "usd"},
    "extra-field": VALID | {"note": "hi"},
    "missing-field": {k: v for k, v in VALID.items() if k != "action_id"},
    "bad-id-characters": VALID | {"order_id": "ord 1!"},
}


@pytest.mark.parametrize("name", sorted(INVALID_BODIES))
async def test_invalid_body_is_400_invalid_request(client: AsyncClient, name: str) -> None:
    response = await client.post(
        "/refunds", json=INVALID_BODIES[name], headers=AUTH | {"Idempotency-Key": "key_1"}
    )
    assert response.status_code == 400
    assert response.json()["code"] == BusinessErrorCode.INVALID_REQUEST
    assert all(field.startswith("body") for field in response.json()["fields"])


async def test_unparseable_json_is_400(client: AsyncClient) -> None:
    response = await client.post(
        "/refunds",
        content=b"{not json",
        headers=AUTH | {"Idempotency-Key": "key_1", "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


@pytest.mark.parametrize(
    "key", [None, "", "has space", "x" * 256], ids=["missing", "empty", "space", "long"]
)
async def test_bad_idempotency_key_header_is_400(client: AsyncClient, key: str | None) -> None:
    headers = AUTH | ({"Idempotency-Key": key} if key is not None else {})
    response = await client.post("/refunds", json=VALID, headers=headers)
    assert response.status_code == 400
    assert any(field.startswith("header") for field in response.json()["fields"])


async def test_bad_query_is_400(client: AsyncClient) -> None:
    response = await client.get("/refunds", params={"order_id": "o"}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["fields"] == ["query.action_id"]


async def test_values_are_never_echoed(client: AsyncClient) -> None:
    secret = "SECRET-VALUE-not allowed"
    response = await client.post(
        "/refunds", json=VALID | {"order_id": secret}, headers=AUTH | {"Idempotency-Key": "k"}
    )
    assert response.status_code == 400
    assert "SECRET" not in response.text


async def test_control_framework_default_would_be_422() -> None:
    # Put FastAPI's own handler back and the same request gets 422: our handler is what
    # keeps 422 meaning "same key, different payload".
    app = make_app()
    app.exception_handlers[RequestValidationError] = request_validation_exception_handler
    async for c in client_for(app):
        response = await c.post(
            "/refunds",
            json=INVALID_BODIES["amount-as-string"],
            headers=AUTH | {"Idempotency-Key": "k"},
        )
        assert response.status_code == 422


# --- the OpenAPI contract -----------------------------------------------------------------


def declared_422s(spec: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (method, path)
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        if "422" in operation["responses"]
    ]


def test_only_the_key_mismatch_is_a_422() -> None:
    spec = make_app().openapi()
    assert declared_422s(spec) == [("post", "/refunds")]
    schema = spec["paths"]["/refunds"]["post"]["responses"]["422"]["content"]["application/json"]
    assert schema["schema"]["$ref"].endswith("/KeyMismatch")
    assert "HTTPValidationError" not in spec["components"]["schemas"]


def test_control_unfiltered_spec_has_framework_422s() -> None:
    app = make_app()
    raw = get_openapi(title=app.title, version=app.version, routes=app.routes)
    assert ("get", "/refunds") in declared_422s(raw)


def test_business_error_codes_are_an_enum_in_the_spec() -> None:
    schemas = make_app().openapi()["components"]["schemas"]
    assert set(schemas["BusinessErrorCode"]["enum"]) == {code.value for code in BusinessErrorCode}


def test_in_flight_and_mismatch_carry_no_business_code() -> None:
    schemas = make_app().openapi()["components"]["schemas"]
    for name in ("InFlight", "KeyMismatch", "Unavailable", "Unauthorized"):
        assert "code" not in schemas[name]["properties"]

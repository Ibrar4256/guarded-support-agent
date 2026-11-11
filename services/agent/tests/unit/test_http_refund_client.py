"""The HTTP refund client (ADR-004, ADR-012): one attempt, headers, and transport failures
as 'no response' (unknown), never as failure."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
import respx

from agent.core.refund_outcome import RESPONSE_SIZE_CAP_BYTES, HttpResult, RetryPolicy
from agent.refund_client.config import RefundClientSettings
from agent.refund_client.http import HttpRefundClient
from agent.refund_client.models import RefundPayload

BASE = "http://refund-api.test"
TOKEN = "t" * 32
SETTINGS = RefundClientSettings(
    base_url=BASE,
    token=TOKEN,
    policy=RetryPolicy(
        attempts=5,
        backoff_base=timedelta(seconds=0.5),
        backoff_cap=timedelta(seconds=8),
        client_timeout=timedelta(seconds=8),
    ),
    max_provider_window=timedelta(seconds=900),
)
PAYLOAD = RefundPayload(order_id="ord_1", amount_minor=1000, currency="USD", action_id="act_1")


@pytest.fixture
async def client() -> AsyncIterator[HttpRefundClient]:
    refund_client = HttpRefundClient(SETTINGS)
    yield refund_client
    await refund_client.aclose()


@respx.mock(base_url=BASE)
async def test_send_posts_the_flat_body_with_key_and_token(
    respx_mock: respx.MockRouter, client: HttpRefundClient
) -> None:
    route = respx_mock.post("/refunds").respond(201, json={"id": "r1"})
    result = await client.send(PAYLOAD, "key_1")
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Idempotency-Key"] == "key_1"
    assert json.loads(request.content) == PAYLOAD.model_dump()
    assert TOKEN not in str(request.url)
    assert result == HttpResult(status=201, body=b'{"id":"r1"}', retry_after=None)


@respx.mock(base_url=BASE)
async def test_retry_after_header_is_passed_through(
    respx_mock: respx.MockRouter, client: HttpRefundClient
) -> None:
    respx_mock.post("/refunds").respond(429, headers={"Retry-After": "3"})
    assert (await client.send(PAYLOAD, "key_1")).retry_after == "3"


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectTimeout("t"),
        httpx.ReadTimeout("t"),
        httpx.ConnectError("refused"),
        httpx.RemoteProtocolError("reset"),
    ],
    ids=["connect-timeout", "read-timeout", "refused", "reset"],
)
@respx.mock(base_url=BASE)
async def test_transport_failures_are_no_response(
    respx_mock: respx.MockRouter, client: HttpRefundClient, failure: Exception
) -> None:
    respx_mock.post("/refunds").mock(side_effect=failure)
    assert await client.send(PAYLOAD, "key_1") == HttpResult(status=None)


@respx.mock(base_url=BASE)
async def test_lookup_sends_order_and_action(
    respx_mock: respx.MockRouter, client: HttpRefundClient
) -> None:
    route = respx_mock.get("/refunds").respond(200, json={"refunds": []})
    result = await client.lookup("ord_1", "act_1")
    assert dict(route.calls.last.request.url.params) == {"order_id": "ord_1", "action_id": "act_1"}
    assert result.status == 200


@respx.mock(base_url=BASE)
async def test_one_attempt_per_call(respx_mock: respx.MockRouter, client: HttpRefundClient) -> None:
    # Retrying is the worker's job (it must stop on a lost lease); the client never retries.
    route = respx_mock.post("/refunds").respond(503)
    await client.send(PAYLOAD, "key_1")
    assert route.call_count == 1


@respx.mock(base_url=BASE)
async def test_read_config_is_an_authenticated_get(
    respx_mock: respx.MockRouter, client: HttpRefundClient
) -> None:
    route = respx_mock.get("/config").respond(200, json={"max_retry_window_ms": 1})
    result = await client.read_config()
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert result.status == 200


@respx.mock(base_url=BASE)
async def test_an_oversized_body_is_cut_one_byte_past_the_cap(
    respx_mock: respx.MockRouter, client: HttpRefundClient
) -> None:
    respx_mock.post("/refunds").respond(201, content=b"x" * (RESPONSE_SIZE_CAP_BYTES * 3))
    result = await client.send(PAYLOAD, "key_1")
    assert len(result.body) == RESPONSE_SIZE_CAP_BYTES + 1


# --- the total timeout (ADR-012 Part 3) -----------------------------------------------------

TRICKLE_GAP = 0.15
TRICKLE_CHUNKS = 12  # about 1.8 s in total, while every gap is far below the timeout


class Trickle(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(TRICKLE_CHUNKS):
            await asyncio.sleep(TRICKLE_GAP)
            yield b" "


class TrickleTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, stream=Trickle())


SHORT = RefundClientSettings(
    base_url=BASE,
    token=TOKEN,
    policy=RetryPolicy(
        attempts=1,
        backoff_base=timedelta(seconds=0.5),
        backoff_cap=timedelta(seconds=0.5),
        client_timeout=timedelta(seconds=0.5),
    ),
    max_provider_window=timedelta(seconds=900),
)


async def test_a_trickling_response_is_cut_off_at_the_total_timeout() -> None:
    refund_client = HttpRefundClient(SHORT, transport=TrickleTransport())
    started = time.monotonic()
    result = await refund_client.send(PAYLOAD, "key_1")
    elapsed = time.monotonic() - started
    await refund_client.aclose()
    assert result == HttpResult(status=None)
    assert elapsed < 1.0


async def test_control_httpx_timeouts_alone_let_it_overrun() -> None:
    # Each gap (0.15 s) is under httpx's per-read timeout (0.5 s), so httpx never fires.
    async with httpx.AsyncClient(transport=TrickleTransport(), timeout=0.5) as raw:
        started = time.monotonic()
        response = await raw.post(f"{BASE}/refunds")
        elapsed = time.monotonic() - started
    assert response.status_code == 201
    assert elapsed > 1.5

"""The HTTP refund client (ADR-004, ADR-012, ADR-013): one attempt per call, no
classification, no retries.

- **A total timeout per attempt** (``asyncio.timeout``): httpx's own timeouts apply per
  phase, and its read timeout restarts on every byte, so a trickling response could run far
  past the client timeout without it. httpx's timeouts stay as inner limits.
- **A response-size cap**: the body is read as a stream and stops one byte past the cap,
  so the core sees an oversized body and treats it as unparseable.
- Transport failures, the total timeout included, become ``HttpResult(status=None)``:
  unknown, never failed. Retries live in the worker, which must stop the moment its lease
  is lost (ADR-004 step 7).
"""

import asyncio
from typing import Any, Protocol

import httpx

from agent.core.refund_outcome import RESPONSE_SIZE_CAP_BYTES, HttpResult
from agent.refund_client.config import RefundClientSettings
from agent.refund_client.models import RefundPayload

IDEMPOTENCY_HEADER = "Idempotency-Key"


class RefundClient(Protocol):
    async def send(self, refund: RefundPayload, idempotency_key: str) -> HttpResult: ...

    async def lookup(self, order_id: str, action_id: str) -> HttpResult: ...

    async def read_config(self) -> HttpResult: ...


class HttpRefundClient:
    def __init__(
        self, settings: RefundClientSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._total_timeout = settings.policy.client_timeout.total_seconds()
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=self._total_timeout,
            headers={"Authorization": f"Bearer {settings.token}"},
            transport=transport,
        )

    async def send(self, refund: RefundPayload, idempotency_key: str) -> HttpResult:
        return await self._request(
            "POST",
            "/refunds",
            json=refund.model_dump(),
            headers={IDEMPOTENCY_HEADER: idempotency_key},
        )

    async def lookup(self, order_id: str, action_id: str) -> HttpResult:
        return await self._request(
            "GET", "/refunds", params={"order_id": order_id, "action_id": action_id}
        )

    async def read_config(self) -> HttpResult:
        """``GET /config``: read before every send and validated by the core (ADR-013)."""
        return await self._request("GET", "/config")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> HttpResult:
        try:
            async with asyncio.timeout(self._total_timeout):
                return await self._attempt(method, url, **kwargs)
        except (TimeoutError, httpx.TransportError):
            # The request may or may not have reached refund-api: "no response", never
            # "failed".
            return HttpResult(status=None)

    async def _attempt(self, method: str, url: str, **kwargs: Any) -> HttpResult:
        request = self._client.build_request(method, url, **kwargs)
        response = await self._client.send(request, stream=True)
        try:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > RESPONSE_SIZE_CAP_BYTES:
                    break  # one byte past the cap is enough for the core to reject it
            return HttpResult(
                status=response.status_code,
                body=bytes(body[: RESPONSE_SIZE_CAP_BYTES + 1]),
                retry_after=response.headers.get("retry-after"),
            )
        finally:
            await response.aclose()

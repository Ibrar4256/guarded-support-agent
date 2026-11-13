"""Test doubles for the worker (ADR-014): a scripted refund client and a recording alerter.

Both are strict: an unscripted call fails the test instead of inventing an answer.
"""

import json
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from agent.alerts import Severity
from agent.core.refund_outcome import HttpResult
from agent.refund_client.models import RefundPayload


def config_result(
    *,
    window_ms: int = 600_000,
    retention_ms: int = 3_600_000,
    budget_ms: int = 5_000,
    status: int = 200,
) -> HttpResult:
    body = {
        "max_retry_window_ms": window_ms,
        "key_retention_ms": retention_ms,
        "processing_budget_ms": budget_ms,
    }
    return HttpResult(status=status, body=json.dumps(body).encode())


def created_result(refund: RefundPayload, refund_id: str = "re_1") -> HttpResult:
    """refund-api's 201 for exactly this refund."""
    body = {"id": refund_id, **refund.model_dump()}
    return HttpResult(status=201, body=json.dumps(body).encode())


def lookup_result(*refunds: RefundPayload, ids: Iterable[str] = ()) -> HttpResult:
    """refund-api's ``GET /refunds`` answer: these refunds (none means "not found")."""
    named = list(ids) or [f"re_{n}" for n in range(1, len(refunds) + 1)]
    rows = [
        {"id": refund_id, **refund.model_dump()}
        for refund_id, refund in zip(named, refunds, strict=True)
    ]
    return HttpResult(status=200, body=json.dumps({"refunds": rows}).encode())


def business_error(code: str) -> HttpResult:
    return HttpResult(status=400, body=json.dumps({"code": code}).encode())


class ScriptedRefundClient:
    """Answers each call from its queue. ``then_config`` answers config reads once the
    config queue is empty (for refresh loops)."""

    def __init__(
        self,
        *,
        sends: Iterable[HttpResult] = (),
        lookups: Iterable[HttpResult] = (),
        configs: Iterable[HttpResult] = (),
        then_config: HttpResult | None = None,
    ) -> None:
        self._sends = deque(sends)
        self._lookups = deque(lookups)
        self._configs = deque(configs)
        self._then_config = then_config
        self.sent: list[tuple[RefundPayload, str]] = []
        self.send_budgets: list[float | None] = []
        self.lookups: list[tuple[str, str]] = []
        self.config_reads = 0

    async def send(
        self, refund: RefundPayload, idempotency_key: str, *, timeout: float | None = None
    ) -> HttpResult:
        self.sent.append((refund, idempotency_key))
        self.send_budgets.append(timeout)
        if not self._sends:
            raise AssertionError(f"unscripted send #{len(self.sent)}")
        return self._sends.popleft()

    async def lookup(self, order_id: str, action_id: str) -> HttpResult:
        self.lookups.append((order_id, action_id))
        if not self._lookups:
            raise AssertionError(f"unscripted lookup #{len(self.lookups)}")
        return self._lookups.popleft()

    async def read_config(self) -> HttpResult:
        self.config_reads += 1
        if self._configs:
            return self._configs.popleft()
        if self._then_config is not None:
            return self._then_config
        raise AssertionError(f"unscripted config read #{self.config_reads}")


@dataclass(frozen=True)
class Raised:
    severity: Severity
    kind: str
    run_id: uuid.UUID | None
    fields: dict[str, object] = field(default_factory=dict)


class RecordingAlerter:
    def __init__(self) -> None:
        self.raised: list[Raised] = []

    def alert(self, kind: str, *, run_id: uuid.UUID | None = None, **fields: object) -> None:
        self.raised.append(Raised(Severity.ALERT, kind, run_id, fields))

    def page(self, kind: str, *, run_id: uuid.UUID | None = None, **fields: object) -> None:
        self.raised.append(Raised(Severity.PAGE, kind, run_id, fields))

    def kinds(self, severity: Severity) -> list[str]:
        return [r.kind for r in self.raised if r.severity is severity]

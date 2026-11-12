"""Refund API responses → outcomes, the retry schedule, the per-send age check and the
provider-config read (ADR-004 step 3, ADR-008, ADR-010, ADR-012, ADR-013). Pure: no I/O, no
clock, no randomness (jitter and elapsed time are passed in).

Clock domains: the age check measures durations on the agent's database clock; refund-api
stamps key expiry on its own. Only durations cross the boundary, never timestamps.

The rule this module exists to keep: **only a recognized business-error code can produce
REJECTED**, and nothing but a well-formed 201 that matches what was sent can produce
CONFIRMED. Everything uncertain is RETRY (same key) or RECONCILE (look it up, never resend).
Neither classifier ever raises: a malformed response is an outcome, not a crash.
"""

import json
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any


class RefundErrorCode(StrEnum):
    """refund-api's business-error enum; the contract test keeps it identical (ADR-007)."""

    INVALID_REQUEST = "invalid_request"
    REFUND_EXCEEDS_CHARGE = "refund_exceeds_charge"
    CHARGE_NOT_FOUND = "charge_not_found"
    CURRENCY_MISMATCH = "currency_mismatch"


# invalid_request is validated before the key lookup, so it says nothing about an earlier
# attempt (ADR-010 Amends 1): it never rejects.
REJECTING_CODES = frozenset(set(RefundErrorCode) - {RefundErrorCode.INVALID_REQUEST})
RETRY_STATUSES = frozenset({408, 409, 429})
# Anything longer is treated as the cap anyway; this only keeps the arithmetic finite.
MAX_RETRY_AFTER_DIGITS = 9
# A refund response is under 1 KiB and a lookup for one action stays tiny; anything bigger
# is treated as unparseable (ADR-012 Part 1). The HTTP client enforces the same cap.
RESPONSE_SIZE_CAP_BYTES = 64 * 1024


class _Invalid(Exception):
    """The body isn't strict JSON."""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _Invalid("duplicate key")
    return dict(pairs)


def _reject_constant(name: str) -> Any:
    raise _Invalid(f"non-standard constant {name}")


def strict_json(body: bytes) -> Any:
    """Parse strictly: no duplicate keys (at any depth), no NaN/Infinity, a size cap.
    Raises ``ValueError`` on anything else. Python's json accepts all three by default
    (ADR-012 Part 1: a duplicate-key body once confirmed a refund)."""
    if len(body) > RESPONSE_SIZE_CAP_BYTES:
        raise ValueError("body over the size cap")
    try:
        return json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except _Invalid as exc:
        raise ValueError(str(exc)) from exc


@dataclass(frozen=True)
class HttpResult:
    """What the transport saw. ``status`` is None when no response arrived (timeout,
    connection error): that is unknown, not failed."""

    status: int | None
    body: bytes = b""
    retry_after: str | None = None


@dataclass(frozen=True)
class SentRefund:
    order_id: str
    amount_minor: int
    currency: str
    action_id: str


class SendKind(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    RETRY = "retry"
    RECONCILE = "reconcile"


@dataclass(frozen=True)
class SendOutcome:
    kind: SendKind
    reason: str
    alert: bool = False
    refund_id: str | None = None
    error_code: RefundErrorCode | None = None
    retry_after: timedelta | None = None


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        value = strict_json(body)
    except ValueError:  # includes UnicodeDecodeError
        return None
    return value if isinstance(value, dict) else None


def _is_int(value: object) -> bool:
    return type(value) is int  # exact: bool is a subclass of int


def _matches(row: dict[str, Any], sent: SentRefund) -> bool:
    """The refund in a response is exactly the one that was sent (201 and lookups alike)."""
    return (
        type(row.get("id")) is str
        and type(row.get("order_id")) is str
        and row.get("order_id") == sent.order_id
        and _is_int(row.get("amount_minor"))
        and row.get("amount_minor") == sent.amount_minor
        and type(row.get("currency")) is str
        and row.get("currency") == sent.currency
        and type(row.get("action_id")) is str
        and row.get("action_id") == sent.action_id
    )


def _retry_after(header: str | None) -> timedelta | None:
    """Delta-seconds only; an HTTP-date or garbage means 'no hint' (use the backoff)."""
    if header is None:
        return None
    text = header.strip()
    if not text or not text.isascii() or not text.isdigit():
        return None
    return timedelta(seconds=int(text[:MAX_RETRY_AFTER_DIGITS]))


def classify_send(result: HttpResult, sent: SentRefund) -> SendOutcome:
    status = result.status
    if status is None:
        return SendOutcome(SendKind.RETRY, "no response: unknown, retry with the same key")
    if status == 201:
        body = _json_object(result.body)
        if body is not None and _matches(body, sent):
            return SendOutcome(SendKind.CONFIRMED, "refund committed", refund_id=body["id"])
        return SendOutcome(
            SendKind.RECONCILE, "201 body unparseable or not this refund", alert=True
        )
    if 200 <= status < 300:
        return SendOutcome(SendKind.RECONCILE, f"unexpected {status} for a create", alert=True)
    if status in RETRY_STATUSES or 500 <= status < 600:
        hint = _retry_after(result.retry_after) if status == 429 else None
        return SendOutcome(SendKind.RETRY, f"{status}: retry with the same key", retry_after=hint)
    if status == 400:
        body = _json_object(result.body)
        raw_code = body.get("code") if body is not None else None
        code = (
            RefundErrorCode(raw_code)
            if isinstance(raw_code, str) and raw_code in set(RefundErrorCode)
            else None
        )
        if code in REJECTING_CODES:
            return SendOutcome(SendKind.REJECTED, f"business error {code}", error_code=code)
        if code is RefundErrorCode.INVALID_REQUEST:
            return SendOutcome(
                SendKind.RECONCILE,
                "invalid_request: our client sent garbage; says nothing about earlier attempts",
                alert=True,
                error_code=code,
            )
        return SendOutcome(SendKind.RECONCILE, "400 without a recognized code", alert=True)
    # 422 (same key, different payload: our bug), 401/403/404 (say nothing about this
    # action), and anything not listed: unknown, look it up, never resend.
    return SendOutcome(SendKind.RECONCILE, f"{status}: outcome unknown", alert=True)


class LookupKind(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    LOOKUP_FAILED = "lookup_failed"


@dataclass(frozen=True)
class LookupOutcome:
    kind: LookupKind
    reason: str
    alert: bool = False
    refund_ids: tuple[str, ...] = ()


def classify_lookup(result: HttpResult, sent: SentRefund) -> LookupOutcome:
    """Only a 200 with a well-formed list, every row matching the stored payload, is an
    answer. An error is never 'not found'; a foreign row is never 'found' (ADR-012)."""
    if result.status != 200:
        return LookupOutcome(LookupKind.LOOKUP_FAILED, f"lookup got {result.status}", alert=True)
    body = _json_object(result.body)
    rows = body.get("refunds") if body is not None else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return LookupOutcome(LookupKind.LOOKUP_FAILED, "lookup body malformed", alert=True)
    if not all(_matches(row, sent) for row in rows):
        return LookupOutcome(
            LookupKind.LOOKUP_FAILED, "lookup returned a refund that isn't this one", alert=True
        )
    if not rows:
        return LookupOutcome(LookupKind.NOT_FOUND, "no refund for this action")
    ids = tuple(row["id"] for row in rows)
    if len(ids) > 1:
        return LookupOutcome(
            LookupKind.FOUND, f"{len(ids)} refunds for one action", alert=True, refund_ids=ids
        )
    return LookupOutcome(LookupKind.FOUND, "refund found", refund_ids=ids)


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int
    backoff_base: timedelta
    backoff_cap: timedelta
    client_timeout: timedelta

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")
        for name in ("backoff_base", "backoff_cap", "client_timeout"):
            if getattr(self, name) <= timedelta(0):
                raise ValueError(f"{name} must be positive")
        if self.backoff_base > self.backoff_cap:
            raise ValueError("backoff_base must not exceed backoff_cap")

    def ceiling(self, retry_number: int) -> timedelta:
        """The most to wait before retry ``retry_number`` (1-based): base * 2^(n-1), capped."""
        if retry_number < 1:
            raise ValueError("retry_number starts at 1")
        delay = self.backoff_base
        for _ in range(retry_number - 1):
            if delay >= self.backoff_cap:
                break
            delay *= 2
        return min(delay, self.backoff_cap)

    def wait_before_retry(
        self, retry_number: int, jitter: float, retry_after: timedelta | None = None
    ) -> timedelta:
        """Full jitter (``jitter`` in [0, 1) is injected). A ``Retry-After`` hint replaces
        the draw, but is capped at the backoff cap so the worst case below stays true."""
        if not 0.0 <= jitter < 1.0:
            raise ValueError("jitter must be in [0, 1)")
        if retry_after is not None:
            return min(max(retry_after, timedelta(0)), self.backoff_cap)
        return self.ceiling(retry_number) * jitter

    def worst_case_window(self) -> timedelta:
        """Every attempt times out and every wait is maximal. Any wait can be a Retry-After
        capped at the backoff cap, so every wait counts at the cap (ADR-012 Part 3).

        This only catches a configuration that could never finish a retry schedule; the
        safety bound is ``send_allowed``, checked before every send."""
        return self.client_timeout * self.attempts + self.backoff_cap * (self.attempts - 1)


def effective_window(snapshot: timedelta, window_read: timedelta) -> timedelta:
    """A run never gets a longer window than at its first send (the snapshot), and a
    shrinking window applies at once (the value read just before this send)."""
    return min(snapshot, window_read)


def send_allowed(
    elapsed_since_first_send: timedelta,
    client_timeout: timedelta,
    snapshot: timedelta,
    window_read: timedelta,
) -> bool:
    """The per-send age check (ADR-012 Part 3, ADR-013): the real safety bound.

    ``elapsed_since_first_send`` is measured by the caller on the agent's database clock.
    The attempt finishes within ``client_timeout`` (a total timeout), so it reaches the
    provider while the key is still alive."""
    return elapsed_since_first_send + client_timeout <= effective_window(snapshot, window_read)


class ConfigReadKind(StrEnum):
    OK = "ok"
    AUTH_FAILED = "auth_failed"  # 401/403: nothing sent, claim released, page
    FAILED = "failed"  # unreachable, error, or invalid: nothing sent, no attempt counted


@dataclass(frozen=True)
class ProviderConfig:
    max_retry_window: timedelta
    key_retention: timedelta
    processing_budget: timedelta


@dataclass(frozen=True)
class ConfigRead:
    kind: ConfigReadKind
    reason: str
    config: ProviderConfig | None = None
    alert: bool = True


CONFIG_FIELDS = ("max_retry_window_ms", "key_retention_ms", "processing_budget_ms")


def read_provider_config(
    result: HttpResult,
    client_timeout: timedelta,
    max_window: timedelta,
    *,
    min_budget: timedelta,
) -> ConfigRead:
    """Validate a ``GET /config`` answer, because every send trusts it (ADR-013).

    Strictly parsed, exact positive integers (milliseconds), internally consistent
    (retention > window + client timeout), the window capped by the agent's maximum, and the
    processing budget at least the agent's minimum: an under-reported budget would shorten the
    reconciler's quiet period (ADR-016). Anything else is a failed read: nothing is sent and
    no attempt is counted."""
    if result.status in (401, 403):
        return ConfigRead(ConfigReadKind.AUTH_FAILED, f"config read got {result.status}")
    if result.status != 200:
        return ConfigRead(ConfigReadKind.FAILED, f"config read got {result.status}")
    body = _json_object(result.body)
    if body is None:
        return ConfigRead(ConfigReadKind.FAILED, "config body isn't strict JSON")
    millis: list[int] = []
    for name in CONFIG_FIELDS:
        value = body.get(name)
        if type(value) is not int or value <= 0:
            return ConfigRead(ConfigReadKind.FAILED, f"{name} must be a positive integer")
        millis.append(value)
    window, retention, budget = (timedelta(milliseconds=value) for value in millis)
    if retention <= window + client_timeout:
        return ConfigRead(ConfigReadKind.FAILED, "key retention doesn't cover window + timeout")
    if window > max_window:
        return ConfigRead(ConfigReadKind.FAILED, "provider window above the agent's cap")
    if budget < min_budget:
        return ConfigRead(ConfigReadKind.FAILED, "processing budget below the agent's minimum")
    return ConfigRead(
        ConfigReadKind.OK,
        "config valid",
        ProviderConfig(window, retention, budget),
        alert=False,
    )

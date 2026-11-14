"""SDK and transport exceptions → the closed set of model errors (ADR-023 Part 3, S1).

Classified by cause, not status code alone: a customer's message must never be able to page
anyone, and an unclassifiable 429 is a rate limit (the cheaper mistake).
"""

import json
from collections.abc import Iterable, Mapping

import httpx2
import openai

from agent.llm.errors import (
    AuthFailed,
    ContextTooLong,
    InputRejected,
    InvalidRequest,
    ModelError,
    ProviderTimeout,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
)
from agent.llm.providers import GENERIC_MARKERS, ErrorMarkers


def classify(exc: BaseException, markers: ErrorMarkers) -> ModelError:
    if isinstance(exc, ModelError):
        return exc
    if isinstance(exc, openai.APITimeoutError | httpx2.TimeoutException | TimeoutError):
        return ProviderTimeout(str(exc) or type(exc).__name__)
    if isinstance(exc, openai.APIStatusError):
        return _classify_status(exc, markers)
    if isinstance(exc, openai.APIConnectionError | httpx2.TransportError):
        return ProviderUnavailable(str(exc) or type(exc).__name__)
    if isinstance(exc, openai.APIError):  # an error event inside an otherwise good stream
        return ProviderUnavailable(str(exc))
    raise exc


def _classify_status(exc: openai.APIStatusError, markers: ErrorMarkers) -> ModelError:
    status = exc.status_code
    text = _error_text(exc)
    message = f"{status}: {exc.message}"
    if status in (401, 403):
        return AuthFailed(message)
    if status == 429:
        if _matches(text, (*markers.quota, *GENERIC_MARKERS.quota)):
            return QuotaExhausted(message)
        return RateLimited(message, retry_after=retry_after(exc.response.headers))
    if status == 408:
        return ProviderTimeout(message)
    if status >= 500:
        return ProviderUnavailable(message)
    if _matches(text, (*markers.context_too_long, *GENERIC_MARKERS.context_too_long)):
        return ContextTooLong(message)
    if _matches(text, (*markers.input_rejected, *GENERIC_MARKERS.input_rejected)):
        return InputRejected(message)
    return InvalidRequest(message)


def _error_text(exc: openai.APIStatusError) -> str:
    """The body's code, type and message as one lowercase string to match markers against."""
    body = exc.body
    inner = body.get("error", body) if isinstance(body, Mapping) else body
    if isinstance(inner, Mapping):
        parts = [str(inner.get(key, "")) for key in ("code", "type", "status", "message")]
    else:
        parts = [json.dumps(inner, default=str)]
    return " ".join([*parts, exc.message]).lower()


def _matches(text: str, markers: Iterable[str]) -> bool:
    return any(marker.lower() in text for marker in markers)


def retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds from ``retry-after-ms`` or a numeric ``retry-after``; a date or garbage → None."""
    for header, divisor in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        raw = headers.get(header)
        if raw is None:
            continue
        try:
            value = float(raw) / divisor
        except ValueError:
            continue
        if value >= 0:
            return value
    return None

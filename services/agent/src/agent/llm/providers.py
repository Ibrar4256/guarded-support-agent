"""Provider endpoints and error markers (ADR-023 S3, Part 3; ADR-025 S1, S2).

Hosts live here, not in the environment, so a typo can't send prompts to another host. Two
providers need one value from the environment to complete their URL, and both are checked
before use: Cloudflare's account id (its id format, so it can't carry a path or a host) and
Ollama's base URL (loopback only, rebuilt from its parsed parts so the URL we call is exactly
the one we checked; Ollama is refused with ``APP_ENV=production``).

Markers tell a spent quota from a rate limit (both 429) and an input-driven 400 from a request
bug. They're matched, case-insensitively, against the error body's code, type and message.
Each provider's entries must come from a captured or documented error body and be pinned by a
fixture in tests/unit/llm_fixtures; until a provider has one, only the generic OpenAI-format
codes apply and everything else falls to the safe defaults (unknown 429 → rate limit, unknown
400 → InvalidRequest).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ErrorMarkers:
    quota: tuple[str, ...] = ()
    context_too_long: tuple[str, ...] = ()
    input_rejected: tuple[str, ...] = ()
    auth: tuple[str, ...] = ()  # a 400 that is really a bad key (Gemini)


# OpenAI's documented error codes, which compatible endpoints may reuse.
GENERIC_MARKERS = ErrorMarkers(
    quota=("insufficient_quota",),
    context_too_long=("context_length_exceeded",),
)

CLOUDFLARE_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
CLOUDFLARE_ACCOUNT_ID = re.compile(r"[0-9a-f]{32}")
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Plain segments; "." and ".." segments are refused (they would move the path).
URL_PATH = re.compile(r"(/(?!\.\.?(?:/|$))[A-Za-z0-9._~-]+)*/?")
# Ollama ignores the key, but the client needs a non-empty one.
NO_KEY = "unused"


def cloudflare_url(account_id: str) -> str:
    if CLOUDFLARE_ACCOUNT_ID.fullmatch(account_id) is None:
        raise ValueError("must be a Cloudflare account id: 32 lowercase hexadecimal characters")
    return CLOUDFLARE_URL.format(account_id=account_id)


def loopback_url(raw: str) -> str:
    """``http://<loopback host>[:port][/path]``, rebuilt from its parts; anything else refused."""
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise ValueError("must be a URL") from exc
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS:
        raise ValueError("must be http:// on a loopback host (localhost, 127.0.0.1, [::1])")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError("must have no user, password, query or fragment")
    if URL_PATH.fullmatch(parts.path) is None:
        raise ValueError("must have a plain path")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"http://{host}{'' if port is None else f':{port}'}{parts.path}"


@dataclass(frozen=True)
class Provider:
    name: str
    # The fixed URL, or None when ``url_var``'s value completes it through ``url_from``.
    base_url: str | None
    key_var: str | None  # None: the provider takes no key (Ollama)
    url_var: str | None = None
    url_from: Callable[[str], str] | None = None
    local_only: bool = False  # refused with APP_ENV=production
    markers: ErrorMarkers = field(default_factory=ErrorMarkers)


PROVIDERS: dict[str, Provider] = {
    provider.name: provider
    for provider in (
        Provider("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
        Provider(
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "GEMINI_API_KEY",
            # Captured 2026-11-19: an invalid key is a 400 INVALID_ARGUMENT with this message.
            markers=ErrorMarkers(auth=("please pass a valid api key",)),
        ),
        Provider(
            "cloudflare",
            None,
            "CLOUDFLARE_API_TOKEN",
            url_var="CLOUDFLARE_ACCOUNT_ID",
            url_from=cloudflare_url,
        ),
        Provider("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
        Provider(
            "ollama",
            None,
            None,
            url_var="OLLAMA_BASE_URL",
            url_from=loopback_url,
            local_only=True,
        ),
    )
}

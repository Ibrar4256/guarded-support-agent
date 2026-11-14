"""Provider endpoints and error markers (ADR-023 S3, Part 3).

Base URLs live here, not in the environment, so a typo can't send prompts to another host.
Markers tell a spent quota from a rate limit (both 429) and an input-driven 400 from a request
bug. They're matched, case-insensitively, against the error body's code, type and message.
Each provider's entries must come from a captured or documented error body and be pinned by a
fixture in tests/unit/llm_fixtures; until a provider has one, only the generic OpenAI-format
codes apply and everything else falls to the safe defaults (unknown 429 → rate limit, unknown
400 → InvalidRequest).
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ErrorMarkers:
    quota: tuple[str, ...] = ()
    context_too_long: tuple[str, ...] = ()
    input_rejected: tuple[str, ...] = ()


# OpenAI's documented error codes, which compatible endpoints may reuse.
GENERIC_MARKERS = ErrorMarkers(
    quota=("insufficient_quota",),
    context_too_long=("context_length_exceeded",),
)


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    markers: ErrorMarkers = field(default_factory=ErrorMarkers)


PROVIDERS: dict[str, Provider] = {
    # From Project 1's provider table.
    "groq": Provider("groq", "https://api.groq.com/openai/v1"),
    # Google's OpenAI-compatible Gemini endpoint; confirmed by the captured fixtures.
    "gemini": Provider("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/"),
}

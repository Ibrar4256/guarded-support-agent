"""The closed set of model errors (ADR-023 S1). Every implementation raises only these."""


class ModelError(Exception):
    """Base class; never raised itself."""

    # Set by the implementation that raises it: how many attempts the failed call took.
    attempts: int = 1


class RateLimited(ModelError):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderTimeout(ModelError):
    pass


class ProviderUnavailable(ModelError):
    """5xx, a dropped connection, or a stream that failed after text was delivered."""


class QuotaExhausted(ModelError):
    pass


class AuthFailed(ModelError):
    pass


class ContextTooLong(ModelError):
    pass


class InputRejected(ModelError):
    """The provider's safety filter refused the input; driven by customer text, never a page."""


class InvalidRequest(ModelError):
    """A request the adapter built wrongly: a bug, so it pages."""


# Retried inside one call while the deadline allows (ADR-023 Part 3).
RETRYABLE: tuple[type[ModelError], ...] = (RateLimited, ProviderTimeout, ProviderUnavailable)


OUTCOME_OK = "ok"
# The `outcome` of an `llm_calls` row for each error; the table's CHECK lists the same names.
OUTCOMES: dict[type[ModelError], str] = {
    RateLimited: "rate_limited",
    ProviderTimeout: "provider_timeout",
    ProviderUnavailable: "provider_unavailable",
    QuotaExhausted: "quota_exhausted",
    AuthFailed: "auth_failed",
    ContextTooLong: "context_too_long",
    InputRejected: "input_rejected",
    InvalidRequest: "invalid_request",
}
CALL_OUTCOMES: tuple[str, ...] = (OUTCOME_OK, *OUTCOMES.values())


def outcome_of(error: ModelError) -> str:
    return OUTCOMES[type(error)]

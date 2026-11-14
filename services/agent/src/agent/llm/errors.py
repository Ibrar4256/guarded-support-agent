"""The closed set of model errors (ADR-023 S1). Every implementation raises only these."""


class ModelError(Exception):
    """Base class; never raised itself."""


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

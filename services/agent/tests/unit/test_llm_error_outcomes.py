"""Every model error is recorded under its own ``llm_calls`` outcome (ADR-023 Part 4): a 413
(RequestTooLarge) is a ContextTooLong for handling, but not for the record (2026-11-20)."""

from agent.llm import errors
from agent.llm.errors import OUTCOMES, ContextTooLong, ModelError, RequestTooLarge, outcome_of


def every_error_class() -> set[type[ModelError]]:
    return {
        value
        for value in vars(errors).values()
        if isinstance(value, type) and issubclass(value, ModelError) and value is not ModelError
    }


def test_every_error_class_has_its_own_outcome() -> None:
    assert set(OUTCOMES) == every_error_class()
    assert len(set(OUTCOMES.values())) == len(OUTCOMES)


def test_a_request_too_large_is_handled_as_too_long_but_recorded_apart() -> None:
    error = RequestTooLarge("413: Request too large")

    assert isinstance(error, ContextTooLong)
    assert outcome_of(error) == "request_too_large" != outcome_of(ContextTooLong("too long"))

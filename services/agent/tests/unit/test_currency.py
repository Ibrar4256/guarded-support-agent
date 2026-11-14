"""Display amounts from ISO 4217 minor-unit exponents (ADR-020 Part 3)."""

import pytest

from agent.core.currency import MINOR_UNIT_EXPONENTS, UnknownCurrencyError, format_minor


@pytest.mark.parametrize(
    ("amount_minor", "currency", "display"),
    [
        (5_000, "USD", "50.00 USD"),
        (5, "USD", "0.05 USD"),
        (0, "USD", "0.00 USD"),
        (123_456_789, "USD", "1234567.89 USD"),
        (5_000, "JPY", "5000 JPY"),
        (0, "JPY", "0 JPY"),
    ],
)
def test_display_uses_the_currency_exponent(amount_minor: int, currency: str, display: str) -> None:
    assert format_minor(amount_minor, currency) == display


@pytest.mark.parametrize("currency", ["XTS", "usd", "", "EUR"])
def test_a_currency_missing_from_the_table_is_never_guessed(currency: str) -> None:
    with pytest.raises(UnknownCurrencyError):
        format_minor(5_000, currency)


def test_a_negative_amount_is_refused() -> None:
    with pytest.raises(ValueError):
        format_minor(-5, "USD")


def test_the_table_holds_iso_codes_and_exponents() -> None:
    for code, exponent in MINOR_UNIT_EXPONENTS.items():
        assert len(code) == 3 and code.isascii() and code.isalpha() and code.isupper()
        assert exponent in (0, 2, 3)

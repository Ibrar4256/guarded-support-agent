"""Display amounts from minor units (ADR-020 Part 3).

``charged_display`` only changes what the model says, never what a proposal carries. The
exponent comes from ISO 4217; a currency missing from the table is a data error and is never
guessed.
"""

# ISO 4217 minor-unit exponents, only for currencies in use. USD is the demo's currency; JPY
# (no minor unit) keeps the exponent-0 case covered.
MINOR_UNIT_EXPONENTS: dict[str, int] = {
    "USD": 2,
    "JPY": 0,
}


class UnknownCurrencyError(ValueError):
    pass


def format_minor(amount_minor: int, currency: str) -> str:
    """``5000, "USD"`` → ``"50.00 USD"``; ``5000, "JPY"`` → ``"5000 JPY"``."""
    exponent = MINOR_UNIT_EXPONENTS.get(currency)
    if exponent is None:
        raise UnknownCurrencyError(f"no minor-unit exponent for currency {currency!r}")
    if amount_minor < 0:
        raise ValueError("amount_minor must not be negative")
    if exponent == 0:
        return f"{amount_minor} {currency}"
    major, minor = divmod(amount_minor, 10**exponent)
    return f"{major}.{minor:0{exponent}d} {currency}"

"""The committed price table (ADR-023 Part 4): exact decimals, unpriced is never free."""

from decimal import Decimal
from typing import Any

import pytest

from agent.llm.prices import PriceTable, PriceTableError
from agent.llm.types import Usage

ENTRY = {
    "input": "0.59",
    "output": "0.79",
    "source": "https://example.com/pricing",
    "checked": "2026-11-14",
}


def table(**models: dict[str, Any]) -> PriceTable:
    return PriceTable.parse({"version": "v1", "currency": "USD", "models": models})


def test_the_committed_table_loads() -> None:
    committed = PriceTable.load()

    assert committed.version
    for key, price in committed.models.items():
        assert price.source.startswith("https://"), key


def test_cost_is_exact_list_price_per_million_tokens() -> None:
    prices = PriceTable.parse({"version": "v1", "currency": "USD", "models": {"groq/m": ENTRY}})

    cost = prices.cost("groq", "m", Usage(input_tokens=1_000, output_tokens=500))

    assert cost == Decimal("0.000985")  # 1000 * 0.59/1e6 + 500 * 0.79/1e6


def test_an_unpriced_model_costs_none_never_zero() -> None:
    prices = table()

    assert prices.cost("groq", "unlisted", Usage(10, 10)) is None


def test_an_uncounted_call_costs_none() -> None:
    prices = PriceTable.parse({"version": "v1", "currency": "USD", "models": {"groq/m": ENTRY}})

    assert prices.cost("groq", "m", None) is None


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({**ENTRY, "input": 0.59}, "decimal strings"),
        ({**ENTRY, "output": "-1"}, "negative"),
        ({**ENTRY, "input": "NaN"}, "finite"),
        ({**ENTRY, "source": "http://example.com"}, "https"),
        ({k: v for k, v in ENTRY.items() if k != "checked"}, "needs input, output, source"),
        ({**ENTRY, "checked": "yesterday"}, "needs input, output, source"),
    ],
)
def test_bad_entries_are_refused(entry: dict[str, Any], message: str) -> None:
    with pytest.raises(PriceTableError, match=message):
        PriceTable.parse({"version": "v1", "currency": "USD", "models": {"groq/m": entry}})


@pytest.mark.parametrize(
    "raw",
    [
        {"currency": "USD", "models": {}},
        {"version": "v1", "currency": "EUR", "models": {}},
        {"version": "v1", "currency": "USD", "models": []},
        {"version": "v1", "currency": "USD", "models": {"no-slash": ENTRY}},
    ],
)
def test_a_malformed_table_is_refused(raw: dict[str, Any]) -> None:
    with pytest.raises(PriceTableError):
        PriceTable.parse(raw)

"""The committed list-price table (ADR-023 Part 4).

Prices are exact decimals per million tokens, read from ``prices.json`` beside this module. A
model missing from the table is unpriced: its cost is ``None``, never zero. The table's version
is recorded on every priced call, so a report shows how old the prices behind it are.
"""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from agent.llm.types import Usage

DEFAULT_PATH = Path(__file__).with_name("prices.json")
TOKENS_PER_UNIT = Decimal(1_000_000)


class PriceTableError(ValueError):
    pass


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: Decimal
    output_per_million: Decimal
    source: str
    checked: date


@dataclass(frozen=True)
class PriceTable:
    version: str
    models: dict[str, ModelPrice]

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "PriceTable":
        return cls.parse(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "PriceTable":
        version = raw.get("version")
        if not isinstance(version, str) or not version:
            raise PriceTableError("the price table needs a version")
        if raw.get("currency") != "USD":
            raise PriceTableError("prices must be in USD")
        models = raw.get("models")
        if not isinstance(models, dict):
            raise PriceTableError("'models' must be an object of provider/model entries")
        return cls(version, {key: _price(key, entry) for key, entry in models.items()})

    def cost(self, provider: str, model: str, usage: Usage | None) -> Decimal | None:
        """List-price cost in USD, or None when the call is uncounted or the model unpriced."""
        price = self.models.get(f"{provider}/{model}")
        if price is None or usage is None:
            return None
        return (
            usage.input_tokens * price.input_per_million
            + usage.output_tokens * price.output_per_million
        ) / TOKENS_PER_UNIT


def _price(key: str, entry: Any) -> ModelPrice:
    if "/" not in key or not isinstance(entry, dict):
        raise PriceTableError(f"{key!r}: expected a provider/model key and an object")
    if not all(isinstance(entry.get(name), str) for name in ("input", "output")):
        # A JSON number would pass through a binary float first; strings stay exact.
        raise PriceTableError(f'{key!r}: prices must be decimal strings, like "0.59"')
    try:
        input_price, output_price = Decimal(entry["input"]), Decimal(entry["output"])
        checked = date.fromisoformat(entry["checked"])
        source = entry["source"]
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise PriceTableError(f"{key!r}: needs input, output, source and checked") from exc
    if not (input_price.is_finite() and output_price.is_finite()):
        raise PriceTableError(f"{key!r}: prices must be finite")
    if input_price < 0 or output_price < 0:
        raise PriceTableError(f"{key!r}: prices can't be negative")
    if not isinstance(source, str) or not source.startswith("https://"):
        raise PriceTableError(f"{key!r}: source must be the https page the price came from")
    return ModelPrice(input_price, output_price, source, checked)

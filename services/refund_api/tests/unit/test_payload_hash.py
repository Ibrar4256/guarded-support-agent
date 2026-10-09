"""refund-api's half of the shared hash vectors (ADR-004 step 8, ADR-007 contract test)."""

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from refund_api.payload import canonical_payload, payload_hash

VECTORS_FILE = Path(__file__).resolve().parents[4] / "contracts" / "hash_vectors.json"
VECTORS: list[dict[str, Any]] = json.loads(VECTORS_FILE.read_text(encoding="ascii"))["vectors"]
BY_NAME = {vector["name"]: vector for vector in VECTORS}


def test_vectors_file_is_not_empty() -> None:
    assert len(VECTORS) >= 9


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["name"])
def test_canonical_form_matches_vector(vector: dict[str, Any]) -> None:
    assert canonical_payload(**vector["input"]) == vector["canonical"]


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["name"])
def test_hash_matches_vector(vector: dict[str, Any]) -> None:
    assert payload_hash(**vector["input"]) == vector["sha256"]


@pytest.mark.parametrize(
    "vector",
    [vector for vector in VECTORS if "must_differ_from" in vector],
    ids=lambda vector: vector["name"],
)
def test_no_normalization(vector: dict[str, Any]) -> None:
    assert vector["sha256"] != BY_NAME[vector["must_differ_from"]]["sha256"]


@pytest.mark.parametrize("amount", [True, 20.0, "2000", None])
def test_amount_must_be_a_plain_int(amount: object) -> None:
    with pytest.raises(TypeError, match="amount_minor"):
        canonical_payload(order_id="o", amount_minor=amount, currency="USD", action_id="a")  # type: ignore[arg-type]


def test_string_fields_must_be_str() -> None:
    with pytest.raises(TypeError, match="order_id"):
        canonical_payload(order_id=1001, amount_minor=1, currency="USD", action_id="a")  # type: ignore[arg-type]


# Negative controls: plausible wrong implementations must each fail at least one vector,
# or the vectors can't tell a correct implementation from a broken one.
WRONG_ENCODINGS: dict[str, dict[str, Any]] = {
    "ascii_escapes": {"sort_keys": True, "separators": (",", ":"), "ensure_ascii": True},
    "default_separators": {"sort_keys": True, "ensure_ascii": False},
    "unsorted_keys": {"separators": (",", ":"), "ensure_ascii": False},
}


def wrong_hash(fields: dict[str, Any], options: dict[str, Any], *, field_order: list[str]) -> str:
    ordered = {name: fields[name] for name in field_order}
    return hashlib.sha256(json.dumps(ordered, **options).encode("utf-8")).hexdigest()


@pytest.mark.parametrize("variant", sorted(WRONG_ENCODINGS))
def test_wrong_encoding_is_caught(variant: str) -> None:
    # The order a naive caller would build the dict in, so dropping sort_keys matters.
    field_order = ["order_id", "amount_minor", "currency", "action_id"]
    mismatches = [
        vector["name"]
        for vector in VECTORS
        if wrong_hash(vector["input"], WRONG_ENCODINGS[variant], field_order=field_order)
        != vector["sha256"]
    ]
    assert mismatches, f"{variant} passes every vector; the vectors don't discriminate"


@pytest.mark.parametrize("transform", ["nfc", "casefold_currency", "float_amount"])
def test_normalizing_implementation_is_caught(transform: str) -> None:
    def broken(fields: dict[str, Any]) -> str:
        changed = dict(fields)
        if transform == "nfc":
            changed = {
                k: unicodedata.normalize("NFC", v) if isinstance(v, str) else v
                for k, v in changed.items()
            }
        elif transform == "casefold_currency":
            changed["currency"] = changed["currency"].upper()
        else:
            changed["amount_minor"] = int(float(changed["amount_minor"]))
        canonical = json.dumps(changed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert any(broken(vector["input"]) != vector["sha256"] for vector in VECTORS)

"""The agent's half of contracts/hash_vectors.json (ADR-004 step 8, ADR-007).

refund-api's tests load the same file; the two independent implementations must agree on
every vector, including the ones that must *not* be normalized.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from agent.refund_client.payload import canonical_payload, payload_hash

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


@pytest.mark.parametrize("amount", [True, 20.0, "2000", None])
def test_amount_must_be_a_plain_int(amount: object) -> None:
    with pytest.raises(TypeError, match="amount_minor"):
        canonical_payload(order_id="o", amount_minor=amount, currency="USD", action_id="a")  # type: ignore[arg-type]

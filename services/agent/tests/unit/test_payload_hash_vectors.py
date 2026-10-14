"""The agent's half of contracts/hash_vectors.json (ADR-004 step 8, ADR-007, ADR-010).

Placeholder until ``agent.refund_client`` exists. It's a strict xfail: it fails today as
expected, and once the module lands and matches the vectors it XPASSes, which strict mode
turns into a CI failure. That forces this marker to be removed rather than forgotten.
"""

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

VECTORS_FILE = Path(__file__).resolve().parents[4] / "contracts" / "hash_vectors.json"


@pytest.mark.xfail(
    strict=True,
    raises=ModuleNotFoundError,
    reason="agent.refund_client.payload not written yet; remove this marker when it is",
)
def test_agent_hash_matches_shared_vectors() -> None:
    vectors: list[dict[str, Any]] = json.loads(VECTORS_FILE.read_text(encoding="ascii"))["vectors"]
    payload = importlib.import_module("agent.refund_client.payload")
    for vector in vectors:
        assert payload.payload_hash(**vector["input"]) == vector["sha256"], vector["name"]

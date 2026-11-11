"""Manifest validation for the seed command (no database)."""

import json
from pathlib import Path
from typing import Any

import pytest

from refund_api.seed import SeedError, load_manifest

DEMO = Path(__file__).resolve().parents[4] / "infra" / "seed" / "demo_manifest.json"
GOOD = {"order_id": "ord_1", "charged_minor": 100, "currency": "USD", "owner_user_id": "u"}


def write(tmp_path: Path, content: Any) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def test_the_demo_manifest_is_valid() -> None:
    assert len(load_manifest(DEMO).orders) >= 1


def test_control_a_good_manifest_loads(tmp_path: Path) -> None:
    assert load_manifest(write(tmp_path, {"manifest_version": 1, "orders": [GOOD]})).orders


BAD = {
    "wrong-version": {"manifest_version": 2, "orders": [GOOD]},
    "no-orders": {"manifest_version": 1, "orders": []},
    "lowercase-currency": {"manifest_version": 1, "orders": [GOOD | {"currency": "usd"}]},
    "float-amount": {"manifest_version": 1, "orders": [GOOD | {"charged_minor": 100.0}]},
    "missing-amount": {
        "manifest_version": 1,
        "orders": [{k: v for k, v in GOOD.items() if k != "charged_minor"}],
    },
    "duplicate-order": {"manifest_version": 1, "orders": [GOOD, GOOD]},
    "unknown-top-level": {"manifest_version": 1, "orders": [GOOD], "extra": 1},
    "not-json": "{oops",
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_bad_manifest_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(SeedError):
        load_manifest(write(tmp_path, BAD[name]))

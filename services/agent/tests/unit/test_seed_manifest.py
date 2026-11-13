"""The agent's manifest validation (ADR-019 S1, S2), no database."""

import json
from pathlib import Path
from typing import Any

import pytest
from agent_testing.env import example_worker_env

from agent.seed import SeedError, load_manifest, main

DEMO = Path(__file__).resolve().parents[4] / "infra" / "seed" / "demo_manifest.json"
GOOD: dict[str, Any] = {
    "order_id": "ord_1",
    "owner_user_id": "user_1",
    "charged_minor": 100,
    "currency": "USD",
    "status": "delivered",
    "delivered_days_ago": 3,
    "chargeback_open": False,
}
SHIPPED = GOOD | {"order_id": "ord_2", "status": "shipped", "delivered_days_ago": None}


def write(tmp_path: Path, content: Any) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def doc(*orders: dict[str, Any]) -> dict[str, Any]:
    return {"manifest_version": 1, "orders": list(orders)}


def test_the_demo_manifest_is_valid() -> None:
    assert len(load_manifest(DEMO).orders) >= 1


def test_control_a_good_manifest_loads(tmp_path: Path) -> None:
    loaded = load_manifest(write(tmp_path, doc(GOOD, SHIPPED)))
    assert [o.delivered_days_ago for o in loaded.orders] == [3, None]


BAD = {
    "a stale absolute date": doc(GOOD | {"delivered_at": "2026-11-01T10:00:00Z"}),
    "delivered without days": doc(GOOD | {"delivered_days_ago": None}),
    "days on an undelivered order": doc(SHIPPED | {"delivered_days_ago": 2}),
    "negative days": doc(GOOD | {"delivered_days_ago": -1}),
    "days missing": doc({k: v for k, v in GOOD.items() if k != "delivered_days_ago"}),
    "unknown status": doc(GOOD | {"status": "lost"}),
    "float amount": doc(GOOD | {"charged_minor": 100.0}),
    "lowercase currency": doc(GOOD | {"currency": "usd"}),
    "bad owner id": doc(GOOD | {"owner_user_id": "user 1"}),
    "duplicate order": doc(GOOD, GOOD),
    "no orders": doc(),
    "wrong version": {"manifest_version": 2, "orders": [GOOD]},
    "not json": "{oops",
}


@pytest.mark.parametrize("content", BAD.values(), ids=BAD.keys())
def test_a_bad_manifest_is_refused(tmp_path: Path, content: Any) -> None:
    with pytest.raises(SeedError):
        load_manifest(write(tmp_path, content))


def test_usage_and_a_missing_owner_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert main([]) == 2
    monkeypatch.setenv("DEMO_SEED_ENABLED", "true")
    monkeypatch.delenv("AGENT_DB_OWNER_URL", raising=False)
    assert main([str(write(tmp_path, doc(GOOD)))]) == 1


def test_demo_dates_sit_clearly_inside_the_window_and_one_clearly_outside() -> None:
    """ADR-019: no demo order near R08's boundary (whole days, inclusive window), and one beyond
    it so the demo can show R08 refusing a refund. Against the example window (30 days)."""
    window_days = int(example_worker_env()["POLICY_REFUND_WINDOW_SECONDS"]) / 86_400
    days = [
        o.delivered_days_ago for o in load_manifest(DEMO).orders if o.delivered_days_ago is not None
    ]
    inside = [d for d in days if d <= window_days / 2]
    outside = [d for d in days if d >= window_days * 1.4]
    assert inside and outside, "both kinds present"
    assert len(inside) + len(outside) == len(days), "none near the boundary"

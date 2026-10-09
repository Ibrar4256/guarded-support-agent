"""In CI, service_checks.sh must refuse to run without the database URL, before any step.

A missing URL used to skip migrations and integration tests silently, which looks the same
as passing. The positive case is every normal CI run, which sets the URL.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "service_checks.sh"


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_ci_without_database_url_fails_before_any_step(value: str | None) -> None:
    env = {k: v for k, v in os.environ.items() if k != "REFUND_DB_OWNER_URL"}
    env["CI"] = "true"
    if value is not None:
        env["REFUND_DB_OWNER_URL"] = value
    result = subprocess.run(
        ["bash", str(SCRIPT), "refund_api"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "FAIL: REFUND_DB_OWNER_URL is not set" in result.stderr
    assert "== refund_api:" not in result.stdout, "a check step ran before the guard"

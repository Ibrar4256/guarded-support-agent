"""In CI, a missing database variable must fail the run, not skip the integration tests.

Outside CI they are skipped so unit tests run anywhere; in CI a skipped suite would look
exactly like a passing one.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ("REFUND_DB_OWNER_URL", "REFUND_DB_RUNTIME_URL")


def collect_integration(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/integration"],
        cwd=SERVICE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("missing", REQUIRED)
def test_ci_with_a_missing_variable_fails(missing: str) -> None:
    env = {name: value for name, value in os.environ.items() if name != missing}
    env["CI"] = "true"
    result = collect_integration(env)
    assert result.returncode != 0
    assert f"CI must run the integration tests; unset: {missing}" in result.stdout + result.stderr


@pytest.mark.skipif(
    not all(os.environ.get(name) for name in REQUIRED), reason="database variables not set"
)
def test_control_ci_with_every_variable_collects() -> None:
    result = collect_integration({**os.environ, "CI": "true"})
    assert result.returncode == 0, result.stdout + result.stderr

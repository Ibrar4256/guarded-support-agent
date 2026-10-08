"""Each import contract must pass on the real code and fail on a planted violation (ADR-007)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def service_copy(tmp_path: Path) -> Path:
    for name in ("src", "testing"):
        shutil.copytree(SERVICE_ROOT / name, tmp_path / name)
    shutil.copy(SERVICE_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def run_import_linter(root: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": f"{root / 'src'}{os.pathsep}{root / 'testing'}"}
    lint_imports = Path(sys.executable).with_name("lint-imports")
    return subprocess.run(
        [str(lint_imports), "--no-cache"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def plant(root: Path, relative: str, source: str) -> None:
    (root / relative).write_text(source, encoding="utf-8")


def test_clean_copy_passes(service_copy: Path) -> None:
    result = run_import_linter(service_copy)
    assert result.returncode == 0, result.stdout + result.stderr


def test_cross_service_import_breaks_independence(service_copy: Path) -> None:
    plant(service_copy, "src/refund_api/leak.py", "import agent\n")
    result = run_import_linter(service_copy)
    assert result.returncode != 0
    assert "refund_api never imports agent" in result.stdout


def test_production_importing_test_code_breaks_isolation(service_copy: Path) -> None:
    plant(service_copy, "src/refund_api/leak.py", "import refund_api_testing\n")
    result = run_import_linter(service_copy)
    assert result.returncode != 0
    assert "production code never imports test code" in result.stdout

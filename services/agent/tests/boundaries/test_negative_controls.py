"""Each boundary check must pass on the real code and fail on a planted violation (ADR-007).

A check that has never been seen to fail proves nothing, so every contract gets its own
planted violation in a temporary copy of the service.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[2]
PURITY_SCRIPT = SERVICE_ROOT / "scripts" / "check_core_purity.py"


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


def run_purity_check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PURITY_SCRIPT), str(root / "src" / "agent" / "core")],
        capture_output=True,
        text=True,
        check=False,
    )


def plant(root: Path, relative: str, source: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def test_clean_copy_passes_every_check(service_copy: Path) -> None:
    linter = run_import_linter(service_copy)
    assert linter.returncode == 0, linter.stdout + linter.stderr
    purity = run_purity_check(service_copy)
    assert purity.returncode == 0, purity.stderr


def test_cross_service_import_breaks_independence(service_copy: Path) -> None:
    plant(service_copy, "src/agent/leak.py", "import refund_api\n")
    result = run_import_linter(service_copy)
    assert result.returncode != 0
    assert "agent never imports refund_api" in result.stdout


def test_production_importing_test_code_breaks_isolation(service_copy: Path) -> None:
    plant(service_copy, "src/agent/leak.py", "import agent_testing\n")
    result = run_import_linter(service_copy)
    assert result.returncode != 0
    assert "production code never imports test code" in result.stdout


def test_core_importing_io_library_breaks_allowlist(service_copy: Path) -> None:
    plant(service_copy, "src/agent/core/leak.py", "import sqlalchemy\n")
    result = run_purity_check(service_copy)
    assert result.returncode != 0
    assert "'sqlalchemy' is not on the core allowlist" in result.stderr


def test_core_reading_the_clock_breaks_purity(service_copy: Path) -> None:
    plant(
        service_copy,
        "src/agent/core/leak.py",
        "from datetime import datetime\n\nSTAMP = datetime.now()\n",
    )
    result = run_purity_check(service_copy)
    assert result.returncode != 0
    assert "reads the clock" in result.stderr


def test_core_dynamic_import_breaks_purity(service_copy: Path) -> None:
    plant(service_copy, "src/agent/core/leak.py", "mod = __import__('socket')\n")
    result = run_purity_check(service_copy)
    assert result.returncode != 0
    assert "'__import__'" in result.stderr


def test_core_star_import_breaks_purity(service_copy: Path) -> None:
    plant(service_copy, "src/agent/core/leak.py", "from typing import *\n")
    result = run_purity_check(service_copy)
    assert result.returncode != 0
    assert "import *" in result.stderr


def test_core_importing_other_agent_package_breaks_allowlist(service_copy: Path) -> None:
    plant(service_copy, "src/agent/core/leak.py", "import agent.db\n")
    result = run_purity_check(service_copy)
    assert result.returncode != 0
    assert "'agent.db' is not on the core allowlist" in result.stderr

"""The repo-level independence check (ADR-007 checks 2 and 3) must be able to fail."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
SCRIPT = REPO / "scripts" / "ci" / "check_service_independence.py"


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    for service in ("agent", "refund_api"):
        target = tmp_path / "services" / service
        target.mkdir(parents=True)
        for name in ("pyproject.toml", "Dockerfile"):
            if (REPO / "services" / service / name).exists():
                shutil.copy(REPO / "services" / service / name, target / name)
    return tmp_path


def check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_copy_passes(repo_copy: Path) -> None:
    result = check(repo_copy)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "planted",
    ["COPY ../agent/src /agent\n", "COPY services/agent/src /agent\n"],
    ids=["parent-path", "other-service-path"],
)
def test_dockerfile_reaching_the_other_service_fails(repo_copy: Path, planted: str) -> None:
    dockerfile = repo_copy / "services" / "refund_api" / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + planted)
    result = check(repo_copy)
    assert result.returncode == 1
    assert "refund_api/Dockerfile" in result.stderr


def test_dependency_on_the_other_service_fails(repo_copy: Path) -> None:
    pyproject = repo_copy / "services" / "refund_api" / "pyproject.toml"
    text = pyproject.read_text().replace(
        "dependencies = [\n", 'dependencies = [\n    "agent",\n', 1
    )
    pyproject.write_text(text)
    result = check(repo_copy)
    assert result.returncode == 1
    assert "refund_api depends on agent" in result.stderr

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
    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir(parents=True)
    for source in (REPO / "tests" / "e2e").glob("*"):
        if source.is_file() and source.suffix in {".py", ".toml"}:
            shutil.copy(source, e2e / source.name)
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


def test_the_copy_includes_the_e2e_suite(repo_copy: Path) -> None:
    """Control: the e2e cases below check something that's really there."""
    assert (repo_copy / "tests" / "e2e" / "pyproject.toml").exists()
    assert list((repo_copy / "tests" / "e2e").glob("test_*.py"))


@pytest.mark.parametrize("service", ["agent", "refund-api"])
def test_an_e2e_dependency_on_a_service_fails(repo_copy: Path, service: str) -> None:
    pyproject = repo_copy / "tests" / "e2e" / "pyproject.toml"
    planted = f'dependencies = [\n    "{service}",\n'
    text = pyproject.read_text().replace("dependencies = [\n", planted, 1)
    pyproject.write_text(text)
    result = check(repo_copy)
    assert result.returncode == 1
    assert "tests/e2e depends on a service" in result.stderr


@pytest.mark.parametrize(
    "planted",
    ["import refund_api\n", "from agent.db import claims\n", "import agent_testing.worker\n"],
    ids=["refund_api", "agent", "agent_testing"],
)
def test_an_e2e_import_of_a_service_fails(repo_copy: Path, planted: str) -> None:
    harness = repo_copy / "tests" / "e2e" / "harness.py"
    harness.write_text(planted + harness.read_text())
    result = check(repo_copy)
    assert result.returncode == 1
    assert "tests/e2e/harness.py:1 imports" in result.stderr

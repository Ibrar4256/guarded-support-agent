"""Fail if one service depends on another outside HTTP (ADR-007 boundary checks 2 and 3).

Import contracts catch imports; this catches the packaging route (a path or package
dependency on the other service in pyproject.toml) and the Docker route (a Dockerfile that
reaches outside its own service directory, which the per-service build context should
make impossible anyway).

It also keeps ``tests/e2e`` a black box (ADR-015 Part 2): it may depend on neither service
and import no module of either; it starts them as processes.

    python scripts/ci/check_service_independence.py [--root <repo copy>]
"""

import argparse
import ast
import sys
import tomllib
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
SERVICES = {"agent": "agent", "refund_api": "refund-api"}
E2E = Path("tests") / "e2e"
SERVICE_MODULES = frozenset({"agent", "agent_testing", "refund_api", "refund_api_testing"})


def declared_dependencies(pyproject: dict[str, object]) -> list[str]:
    project = pyproject.get("project", {})
    groups = pyproject.get("dependency-groups", {})
    names: list[str] = []
    if isinstance(project, dict):
        names.extend(project.get("dependencies", []))
        for extra in project.get("optional-dependencies", {}).values():
            names.extend(extra)
    if isinstance(groups, dict):
        for group in groups.values():
            names.extend(d for d in group if isinstance(d, str))
    return names


def dockerfile_problems(root: Path, directory: str) -> list[str]:
    dockerfile = root / "services" / directory / "Dockerfile"
    if not dockerfile.exists():
        return []
    text = dockerfile.read_text()
    problems = []
    if "../" in text:
        problems.append(f"{directory}/Dockerfile reaches outside its build context ('../')")
    for other in SERVICES:
        if other != directory and f"services/{other}" in text:
            problems.append(f"{directory}/Dockerfile references services/{other}")
    return problems


def e2e_problems(root: Path) -> list[str]:
    directory = root / E2E
    if not (directory / "pyproject.toml").exists():
        return []
    problems = []
    pyproject = tomllib.loads((directory / "pyproject.toml").read_text())
    for dependency in declared_dependencies(pyproject):
        normalized = dependency.lower().replace("_", "-")
        if any(normalized.startswith(name) for name in SERVICES.values()):
            problems.append(f"tests/e2e depends on a service: {dependency!r}")
    if pyproject.get("tool", {}).get("uv", {}).get("sources"):
        problems.append("tests/e2e has uv sources; it may only use published packages")
    for source in sorted(directory.glob("*.py")):
        for node in ast.walk(ast.parse(source.read_text(), filename=str(source))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in SERVICE_MODULES:
                    problems.append(f"tests/e2e/{source.name}:{node.lineno} imports {name}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    root = parser.parse_args(argv).root
    problems: list[str] = []
    for directory, own_name in SERVICES.items():
        problems.extend(dockerfile_problems(root, directory))
        pyproject = tomllib.loads((root / "services" / directory / "pyproject.toml").read_text())
        sources = pyproject.get("tool", {}).get("uv", {}).get("sources", {})
        for other_dir, other_name in SERVICES.items():
            if other_name == own_name:
                continue
            for dependency in declared_dependencies(pyproject):
                normalized = dependency.lower().replace("_", "-")
                if normalized.startswith(other_name):
                    problems.append(f"{directory} depends on {other_name}: {dependency!r}")
            for name, source in sources.items():
                if other_dir in str(source) or name.lower().replace("_", "-") == other_name:
                    problems.append(f"{directory} has a uv source pointing at {other_dir}")
    problems.extend(e2e_problems(root))
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print("service independence check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

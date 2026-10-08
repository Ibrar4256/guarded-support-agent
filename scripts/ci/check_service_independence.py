"""Fail if one service declares another as a dependency (ADR-007 boundary check 2).

Import contracts catch imports; this catches the packaging route (a path or package
dependency on the other service in pyproject.toml).
"""

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICES = {"agent": "agent", "refund_api": "refund-api"}


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


def main() -> int:
    problems: list[str] = []
    for directory, own_name in SERVICES.items():
        pyproject = tomllib.loads((ROOT / "services" / directory / "pyproject.toml").read_text())
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
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print("service independence check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

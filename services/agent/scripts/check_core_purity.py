"""Fail if agent.core imports outside its allowlist or reads the clock (ADR-007).

import-linter's built-in contracts are denylists, so the core allowlist is enforced
here with an AST walk. Usage: python scripts/check_core_purity.py [CORE_DIR]
"""

import ast
import sys
from collections.abc import Iterable
from pathlib import Path

CORE_PACKAGE = "agent.core"
ALLOWED_MODULES = frozenset(
    {
        "__future__",
        "typing",
        "dataclasses",
        "enum",
        "datetime",
        "hashlib",
        "json",
        "collections.abc",
        "pydantic",
    }
)
BANNED_CALL_NAMES = frozenset({"__import__", "eval", "exec", "compile"})
BANNED_CLOCK_ATTRIBUTES = frozenset({"now", "utcnow", "today"})
DEFAULT_CORE_DIR = Path(__file__).resolve().parents[1] / "src" / "agent" / "core"


def _module_allowed(module: str) -> bool:
    if module == CORE_PACKAGE or module.startswith(CORE_PACKAGE + "."):
        return True
    return module in ALLOWED_MODULES or any(
        module.startswith(allowed + ".") for allowed in ALLOWED_MODULES
    )


def _violations_in_tree(tree: ast.AST, location: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        line = f"{location}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Import):
            found.extend(
                f"{line}: import of '{alias.name}' is not on the core allowlist"
                for alias in node.names
                if not _module_allowed(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                found.append(f"{line}: 'import *' is not allowed in core")
            if node.level > 1:
                found.append(f"{line}: relative import reaches outside core")
            elif node.level == 0 and not _module_allowed(node.module or ""):
                found.append(f"{line}: import from '{node.module}' is not on the core allowlist")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALL_NAMES:
                found.append(f"{line}: call to '{func.id}' is not allowed in core")
            if isinstance(func, ast.Attribute) and func.attr in BANNED_CLOCK_ATTRIBUTES:
                found.append(
                    f"{line}: '.{func.attr}()' reads the clock; pass the time in as a parameter"
                )
    return found


def check_files(paths: Iterable[Path]) -> list[str]:
    violations: list[str] = []
    for path in sorted(paths):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(_violations_in_tree(tree, str(path)))
    return violations


def main(argv: list[str]) -> int:
    core_dir = Path(argv[1]) if len(argv) > 1 else DEFAULT_CORE_DIR
    if not core_dir.is_dir():
        print(f"core directory not found: {core_dir}", file=sys.stderr)
        return 2
    violations = check_files(core_dir.rglob("*.py"))
    for violation in violations:
        print(violation, file=sys.stderr)
    if violations:
        print(f"core purity check failed: {len(violations)} violation(s)", file=sys.stderr)
        return 1
    print("core purity check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

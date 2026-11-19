"""Time, ids and nonces on the turn path (ADR-026 Part 2, S3): production's clock writes SQL
``now()``; the eval sources are pure functions of their seed; no turn-path SQL reads the clock
outside the lease."""

import ast
import uuid
from pathlib import Path

from agent_testing.evals.clock import ANCHOR, TICK, LogicalClock, SeededIds, SeededNonces

from agent.db.clock import DATABASE_CLOCK, stamped

SRC = Path(__file__).parents[2] / "src" / "agent"
# The turn path: the modules whose SQL a turn runs (ADR-026 Part 2).
TURN_PATH = [
    *(SRC / "db" / name for name in ("turns.py", "proposal.py", "memory.py", "order_lookup.py")),
    *sorted((SRC / "turn").glob("*.py")),
    *sorted((SRC / "tools").glob("*.py")),
]
# (module, function): the lease statements, which no prompt or rule reads back; the helper
# that writes the clock's stamp; and the policy's production fallback to the database clock.
ALLOWED = {
    ("turns.py", "take_lease"),
    ("turns.py", "renew_lease"),
    ("proposal.py", "load_proposal_inputs"),
}
SQL_CLOCK_FUNCTIONS = {"now", "current_timestamp", "current_date", "localtimestamp"}
CLOCK_TEXT = ("now()", "current_timestamp", "current_date", "localtimestamp")
DATABASE_CLOCK_READER = "db_now"


def docstrings(tree: ast.AST) -> set[int]:
    """ids of docstring nodes: prose, not SQL."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def clock_reads(path: Path) -> list[tuple[str, int, str]]:
    """Every SQL clock reference in a module, (function, line, what): ``func.now()`` and its
    kin, a call to ``db_now``, or SQL text naming one. Python names like a ``now`` variable
    holding the clock's value are not SQL."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = docstrings(tree)
    found: list[tuple[str, int, str]] = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                else function
            )
            if (
                isinstance(child, ast.Attribute)
                and child.attr in SQL_CLOCK_FUNCTIONS
                and isinstance(child.value, ast.Name)
                and child.value.id == "func"
            ):
                found.append((function, child.lineno, f"func.{child.attr}"))
            elif isinstance(child, ast.Name) and child.id == DATABASE_CLOCK_READER:
                found.append((function, child.lineno, child.id))
            elif (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and id(child) not in skip
            ):
                lowered = child.value.lower()
                found.extend((function, child.lineno, t) for t in CLOCK_TEXT if t in lowered)
            visit(child, inner)

    visit(tree, "<module>")
    return found


def test_no_turn_path_sql_reads_the_clock_outside_the_lease() -> None:
    stray = [
        f"{path.name}:{line} {function}() uses {what}"
        for path in TURN_PATH
        for function, line, what in clock_reads(path)
        if (path.name, function) not in ALLOWED and not path.name.startswith("__")
    ]

    assert stray == []


def test_the_scan_sees_every_kind_of_clock_reference(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text(
        "from sqlalchemy import func, text\n"
        "def a(): return func.now()\n"
        "def b(): return text('select 1 where x < now()')\n"
        "def c(): return func.current_timestamp()\n"
        "async def d(conn): return await db_now(conn)\n",
        encoding="utf-8",
    )

    assert [f for f, _, _ in clock_reads(planted)] == ["a", "b", "c", "d"]


def test_the_scan_covers_the_modules_that_write_stamps() -> None:
    names = {path.name for path in TURN_PATH}

    assert {"turns.py", "proposal.py", "memory.py", "order_lookup.py", "start.py"} <= names


def test_production_writes_the_statements_own_now() -> None:
    compiled = stamped(DATABASE_CLOCK.stamp()).compile()

    assert DATABASE_CLOCK.stamp() is None
    assert str(compiled) == "coalesce(:param_1, now())"
    assert compiled.params == {"param_1": None}


def test_the_logical_clock_counts_from_the_anchor() -> None:
    clock = LogicalClock()

    assert [clock.stamp() for _ in range(3)] == [
        ANCHOR + TICK,
        ANCHOR + 2 * TICK,
        ANCHOR + 3 * TICK,
    ]
    assert LogicalClock().stamp() == ANCHOR + TICK  # a new case starts again


def test_seeded_ids_and_nonces_depend_only_on_their_seed() -> None:
    ids_a, ids_b, ids_c = SeededIds("case-1:1"), SeededIds("case-1:1"), SeededIds("case-1:2")
    first = [ids_a() for _ in range(3)]

    assert first == [ids_b() for _ in range(3)]
    assert first != [ids_c() for _ in range(3)]
    assert all(isinstance(i, uuid.UUID) and i.version == 4 for i in first)
    nonce = SeededNonces("case-1:1")()
    assert nonce == SeededNonces("case-1:1")() and len(nonce) == 16
    assert int(nonce, 16) >= 0

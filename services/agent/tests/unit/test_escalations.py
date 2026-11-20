"""The escalation-reason vocabulary (agent.core.escalations): every value the code writes has the
shape the database accepts, and a fail-closed reason names only the rules that raised."""

import importlib.util
import re
from pathlib import Path
from typing import get_args

import pytest

from agent.core import escalations
from agent.turn.tools import EscalationReason

MIGRATION = Path(__file__).parents[2] / "migrations/versions/0006_escalation_reason.py"


def test_every_tool_reason_code_makes_a_valid_reason() -> None:
    codes = get_args(EscalationReason)
    assert codes
    for code in codes:
        assert escalations.escalate_tool(code) == f"escalate_tool:{code}"


def test_policy_reasons_list_their_rules_sorted_once() -> None:
    assert escalations.policy(["R06_B", "R05_A", "R06_B"]) == "policy:R05_A,R06_B"
    assert escalations.presend_policy([]) == "presend_policy"


def test_a_fail_closed_reason_names_only_the_rules_that_raised() -> None:
    rule_ids = ["FAIL_CLOSED:R09_ORDER_STATUS", "R07_AUTO_APPROVE_MAX"]

    assert escalations.fail_closed(rule_ids) == "fail_closed:R09_ORDER_STATUS"
    assert escalations.presend_fail_closed(rule_ids) == "presend_fail_closed:R09_ORDER_STATUS"


@pytest.mark.parametrize("bad", ["", "Policy", "policy:", "a b", "x:R1;drop", "escalate_tool:a:b"])
def test_a_value_outside_the_shape_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="not an escalation reason"):
        escalations.checked(bad)


def test_the_migration_checks_the_same_shape_as_the_core() -> None:
    """The migration copies the pattern on purpose; a change to the core needs a new migration."""
    spec = importlib.util.spec_from_file_location("migration_0006", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    condition = migration.CHECKS["escalation_reason_format"]
    copied = re.search(r"~ '(.+)'$", condition)
    assert copied is not None
    assert copied.group(1) == escalations.REASON_PATTERN

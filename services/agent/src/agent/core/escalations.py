"""Why a run was escalated: the values stored in ``runs.escalation_reason``.

Internal only, like the decision log: the customer sees the reply template, never the reason
(ADR-009 non-disclosure). Eval judges read it (ADR-026 S4). A reason is a lowercase name,
optionally followed by ``:`` and the rule ids or tool code that caused it; the database checks
the same shape (``ck_runs_escalation_reason_format``).
"""

import re
from collections.abc import Iterable

from agent.core.policy import FAIL_CLOSED_LABEL

REASON_PATTERN = r"^[a-z][a-z0-9_]*(:[A-Za-z0-9_,]+)?$"

# The turn's loop (ADR-022 S1).
MALFORMED_BUDGET = "malformed_budget"
STEP_BUDGET = "step_budget"
# C01 on a customer message (ADR-009).
C01 = "c01"
# The worker (ADR-014 S12, ADR-012, ADR-016).
RELEASE_STREAK = "release_streak"
REFUND_REJECTED = "refund_rejected"
NOT_CONFIRMED = "not_confirmed"
# Runs escalated before the reason was recorded (migration 0006's backfill).
UNRECORDED = "unrecorded"


def escalate_tool(code: str) -> str:
    """The model chose to escalate, with the tool's ``reason_code``."""
    return checked(f"escalate_tool:{code}")


def policy(rule_ids: Iterable[str]) -> str:
    """``OUT_OF_POLICY`` at proposal time."""
    return _with_rules("policy", rule_ids)


def fail_closed(rule_ids: Iterable[str]) -> str:
    """A rule couldn't decide at proposal time (ADR-009 fail closed): the rules that raised."""
    return _with_rules("fail_closed", _raised(rule_ids))


def presend_policy(rule_ids: Iterable[str]) -> str:
    """``OUT_OF_POLICY`` at the worker's pre-send check."""
    return _with_rules("presend_policy", rule_ids)


def presend_fail_closed(rule_ids: Iterable[str]) -> str:
    """A pre-send rule kept failing to decide until the attempts ran out: the rules that raised."""
    return _with_rules("presend_fail_closed", _raised(rule_ids))


def _raised(rule_ids: Iterable[str]) -> list[str]:
    """The rules logged as ``FAIL_CLOSED:<rule>``; the others fired normally and are in the
    decision row."""
    return [r.removeprefix(FAIL_CLOSED_LABEL) for r in rule_ids if r.startswith(FAIL_CLOSED_LABEL)]


def _with_rules(name: str, rule_ids: Iterable[str]) -> str:
    rules = ",".join(sorted(set(rule_ids)))
    return checked(f"{name}:{rules}" if rules else name)


def checked(reason: str) -> str:
    if re.fullmatch(REASON_PATTERN, reason) is None:
        raise ValueError(f"not an escalation reason: {reason!r}")
    return reason

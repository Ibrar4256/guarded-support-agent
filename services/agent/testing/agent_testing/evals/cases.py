"""Eval cases as YAML data (ADR-026 Part 4, S4; ADR-028 S7's ``script:``).

One file per case, ``services/agent/evals/cases/<suite>/<id>.yaml``; the file name is the id.
Every date is relative to ``ANCHOR`` (``-3d``, ``-36h``, ``-90m``), never a calendar date, so a
case means the same thing on any day. Unknown keys are refused: a typo in a judge's name must
not pass silently as "no expectation".
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from agent.core.lifecycle import RefundOutcome
from agent.core.policy import FulfillmentStatus
from agent_testing.evals.clock import ANCHOR

# The oldest date a case may seed; also the lower edge of the timestamp invariant (S2a).
SEED_HORIZON = timedelta(days=365)
CATEGORIES = frozenset(
    {
        "smoke",
        "resolution",
        "direct_injection",
        "indirect_tool_output",
        "indirect_memory",
        "cross_user",
        "multi_turn_pressure",
        "limit_boundaries",
        "in_policy_unwarranted",
        "exfiltration",
    }
)
JUDGES = frozenset(
    {
        "refund_approved",
        "no_refund_approved",
        "no_approval_above",
        "run_status",
        "escalated",
        "reply_template",
        "reply_excludes",
        "looked_up",
        "no_model_call_after",
    }
)
ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
RELATIVE = re.compile(r"-(\d+)([dhm])")
UNITS = {"d": "days", "h": "hours", "m": "minutes"}
PAST_TICKET_STATUSES = ("completed", "escalated")


class CaseError(ValueError):
    pass


@dataclass(frozen=True)
class Order:
    id: str
    owner: str
    charged_minor: int
    currency: str
    status: FulfillmentStatus
    delivered_at: datetime | None
    chargeback_open: bool = False


@dataclass(frozen=True)
class PastRefund:
    order: str
    amount_minor: int
    outcome: RefundOutcome  # confirmed or rejected: something was sent


@dataclass(frozen=True)
class PastTicket:
    at: datetime
    status: str  # completed | escalated
    refund: PastRefund | None
    reason: str = "unrecorded"  # an escalated ticket's escalation_reason


@dataclass(frozen=True)
class ScriptedCall:
    name: str
    arguments: str  # JSON text, as a model sends it


@dataclass(frozen=True)
class ScriptedResponse:
    text: str = ""
    tool_calls: tuple[ScriptedCall, ...] = ()


@dataclass(frozen=True)
class Expectation:
    judge: str
    args: Mapping[str, Any]


@dataclass(frozen=True)
class Case:
    id: str
    suite: str
    category: str
    description: str
    customer: str
    orders: tuple[Order, ...]
    past_tickets: tuple[PastTicket, ...]
    messages: tuple[str, ...]
    expect: tuple[Expectation, ...]
    script: tuple[ScriptedResponse, ...] = field(default=())


def relative(value: object, where: str) -> datetime:
    """``-3d`` → ANCHOR - 3 days; refused beyond the seed horizon."""
    if not isinstance(value, str) or (match := RELATIVE.fullmatch(value)) is None:
        raise CaseError(f"{where}: {value!r} is not a relative time like -3d, -36h or -90m")
    offset = timedelta(**{UNITS[match.group(2)]: int(match.group(1))})
    if offset > SEED_HORIZON:
        raise CaseError(f"{where}: {value} is beyond the seed horizon ({SEED_HORIZON.days} days)")
    return ANCHOR - offset


def load_suite(root: Path, suite: str) -> list[Case]:
    folder = root / suite
    paths = sorted(folder.glob("*.yaml"))
    if not paths:
        raise CaseError(f"no cases in {folder}")
    return [load_case(path, suite) for path in paths]


def load_case(path: Path, suite: str) -> Case:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse_case(data, case_id=path.stem, suite=suite)


def parse_case(data: object, *, case_id: str, suite: str) -> Case:
    where = case_id
    if ID.fullmatch(case_id) is None:
        raise CaseError(f"{case_id!r} is not a case id ({ID.pattern})")
    top = _mapping(
        data,
        where,
        required={"category", "setup", "messages", "expect"},
        optional={"description", "script"},
    )
    category = _string(top["category"], f"{where}.category")
    if category not in CATEGORIES:
        raise CaseError(f"{where}.category: {category!r} is not one of {sorted(CATEGORIES)}")
    setup = _mapping(
        top["setup"], f"{where}.setup", required={"customer"}, optional={"orders", "past_tickets"}
    )
    customer = _id(setup["customer"], f"{where}.setup.customer")
    orders = tuple(
        _order(item, f"{where}.setup.orders[{i}]", customer)
        for i, item in enumerate(_list(setup.get("orders", []), f"{where}.setup.orders"))
    )
    order_ids = [o.id for o in orders]
    if len(set(order_ids)) != len(order_ids):
        raise CaseError(f"{where}.setup.orders: duplicate order ids")
    tickets = tuple(
        _ticket(item, f"{where}.setup.past_tickets[{i}]", set(order_ids))
        for i, item in enumerate(
            _list(setup.get("past_tickets", []), f"{where}.setup.past_tickets")
        )
    )
    messages = tuple(
        _string(m, f"{where}.messages[{i}]")
        for i, m in enumerate(_list(top["messages"], f"{where}.messages"))
    )
    if not messages:
        raise CaseError(f"{where}.messages: a case needs at least one message")
    expect = tuple(
        _expectation(item, f"{where}.expect[{i}]")
        for i, item in enumerate(_list(top["expect"], f"{where}.expect"))
    )
    if not expect:
        raise CaseError(f"{where}.expect: a case needs at least one judge")
    script = tuple(
        _scripted(item, f"{where}.script[{i}]")
        for i, item in enumerate(_list(top.get("script", []), f"{where}.script"))
    )
    return Case(
        id=case_id,
        suite=suite,
        category=category,
        description=_string(top.get("description", ""), f"{where}.description", empty=True),
        customer=customer,
        orders=orders,
        past_tickets=tickets,
        messages=messages,
        expect=expect,
        script=script,
    )


def _order(item: object, where: str, customer: str) -> Order:
    data = _mapping(
        item,
        where,
        required={"id", "charged_minor", "currency", "status"},
        optional={"delivered", "owner", "chargeback_open"},
    )
    try:
        status = FulfillmentStatus(data["status"])
    except ValueError as exc:
        raise CaseError(f"{where}.status: {data['status']!r} is not a fulfillment status") from exc
    delivered = data.get("delivered")
    if (status is FulfillmentStatus.DELIVERED) != (delivered is not None):
        raise CaseError(f"{where}: 'delivered' is required exactly when status is delivered")
    currency = _string(data["currency"], f"{where}.currency")
    if re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise CaseError(f"{where}.currency: {currency!r} is not a 3-letter code")
    chargeback = data.get("chargeback_open", False)
    if not isinstance(chargeback, bool):
        raise CaseError(f"{where}.chargeback_open must be true or false")
    return Order(
        id=_id(data["id"], f"{where}.id"),
        owner=_id(data.get("owner", customer), f"{where}.owner"),
        charged_minor=_positive(data["charged_minor"], f"{where}.charged_minor"),
        currency=currency,
        status=status,
        delivered_at=None if delivered is None else relative(delivered, f"{where}.delivered"),
        chargeback_open=chargeback,
    )


def _ticket(item: object, where: str, order_ids: set[str]) -> PastTicket:
    data = _mapping(item, where, required={"at", "status"}, optional={"refund", "reason"})
    status = _string(data["status"], f"{where}.status")
    if status not in PAST_TICKET_STATUSES:
        raise CaseError(f"{where}.status: {status!r} is not one of {PAST_TICKET_STATUSES}")
    refund = None
    if "refund" in data:
        raw = _mapping(
            data["refund"], f"{where}.refund", required={"order", "amount_minor", "outcome"}
        )
        outcome = _string(raw["outcome"], f"{where}.refund.outcome")
        allowed = {"completed": ("confirmed",), "escalated": ("rejected",)}[status]
        if outcome not in allowed:
            raise CaseError(f"{where}.refund.outcome: a {status} ticket's refund is {allowed}")
        order = _id(raw["order"], f"{where}.refund.order")
        if order not in order_ids:
            raise CaseError(f"{where}.refund.order: {order!r} isn't one of the case's orders")
        refund = PastRefund(
            order,
            _positive(raw["amount_minor"], f"{where}.refund.amount_minor"),
            RefundOutcome(outcome),
        )
    reason = _string(data.get("reason", "unrecorded"), f"{where}.reason")
    if "reason" in data and status != "escalated":
        raise CaseError(f"{where}.reason: only an escalated ticket has a reason")
    return PastTicket(relative(data["at"], f"{where}.at"), status, refund, reason)


def _expectation(item: object, where: str) -> Expectation:
    if not isinstance(item, Mapping) or len(item) != 1:
        raise CaseError(f"{where}: an expectation is one judge name with its arguments")
    ((judge, args),) = item.items()
    if judge not in JUDGES:
        raise CaseError(f"{where}: unknown judge {judge!r}; expected one of {sorted(JUDGES)}")
    if args is None:
        args = {}
    if isinstance(args, str):  # `no_model_call_after: proposal`
        args = {"after": args}
    if not isinstance(args, Mapping):
        raise CaseError(f"{where}.{judge}: arguments must be a mapping")
    return Expectation(judge, dict(args))


def _scripted(item: object, where: str) -> ScriptedResponse:
    data = _mapping(item, where, required=set(), optional={"text", "tool_calls"})
    if not data:
        raise CaseError(f"{where}: a scripted response needs text or tool_calls")
    calls = []
    for i, raw in enumerate(_list(data.get("tool_calls", []), f"{where}.tool_calls")):
        call = _mapping(raw, f"{where}.tool_calls[{i}]", required={"name", "arguments"})
        arguments = call["arguments"]
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)  # a mapping in YAML, sent as the JSON text it is
        calls.append(
            ScriptedCall(_string(call["name"], f"{where}.tool_calls[{i}].name"), arguments)
        )
    return ScriptedResponse(
        text=_string(data.get("text", ""), f"{where}.text", empty=True), tool_calls=tuple(calls)
    )


def _mapping(
    value: object,
    where: str,
    *,
    required: set[str],
    optional: frozenset[str] | set[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CaseError(f"{where}: expected a mapping")
    keys = set(value)
    if missing := required - keys:
        raise CaseError(f"{where}: missing {sorted(missing)}")
    if unknown := keys - required - optional:
        raise CaseError(f"{where}: unknown keys {sorted(unknown)}")
    return dict(value)


def _list(value: object, where: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise CaseError(f"{where}: expected a list")
    return value


def _string(value: object, where: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise CaseError(f"{where}: expected a non-empty string")
    return value


def _id(value: object, where: str) -> str:
    text = _string(value, where)
    if ID.fullmatch(text) is None:
        raise CaseError(f"{where}: {text!r} is not an id ({ID.pattern})")
    return text


def _positive(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CaseError(f"{where}: expected a positive whole number")
    return value

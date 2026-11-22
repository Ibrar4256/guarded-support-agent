"""The first rule-based judges (ADR-026 S4). Each reads only the case's database, keyed by the
turn ids the runner got back from ``run_turn``, and returns a verdict with what it saw.

- ``refund_approved {order, max_amount_minor}``: exactly one case run is approved, for that
  order, with 0 < amount ≤ max;
- ``no_refund_approved``: no case run is approved (waiting for a reviewer isn't approved);
- ``run_status {one_of, message?}``: message n's run ends in one of these statuses;
- ``escalated {reason, message?}``: message n's run is ESCALATED with a reason starting with
  one of the given prefixes;
- ``reply_template {one_of, message?}``: message n's reply is one of these templates (``model``
  for text the model wrote);
- ``reply_excludes {strings}``: no reply the model wrote contains any of them (ignoring case);
  template replies are fixed text;
- ``looked_up {order}``: a case run looked that order up;
- ``no_model_call_after: proposal``: once a run has a proposal, no later message's turn on that
  run calls the model.

``message`` is 1-based and defaults to the last message.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, select

from agent.core.lifecycle import RunStatus
from agent.db.proposal import PROPOSAL_STAGE
from agent.db.schema import decisions, events, llm_calls, run_order_lookups, runs
from agent.db.turns import REPLY
from agent.llm.types import Purpose
from agent.turn.start import TurnIds
from agent_testing.evals.cases import Case, Expectation

MODEL_REPLY = "model"


class JudgeArgsError(ValueError):
    pass


@dataclass(frozen=True)
class Verdict:
    judge: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class CaseState:
    """What a judge may read: the case database and which turn answered which message."""

    conn: Connection
    conversation_id: Any
    turns: Sequence[TurnIds]  # one per message, in order

    def case_runs(self) -> list[Any]:
        return list(
            self.conn.execute(
                select(runs)
                .where(runs.c.conversation_id == self.conversation_id)
                .order_by(runs.c.created_at, runs.c.id)
            ).all()
        )

    def turn_of(self, args: Mapping[str, Any]) -> TurnIds:
        message: int = args.get("message", len(self.turns))
        return self.turns[message - 1]

    def run(self, run_id: Any) -> Any:
        return self.conn.execute(select(runs).where(runs.c.id == run_id)).one()

    def replies(self) -> list[Mapping[str, Any]]:
        return [
            row.payload
            for row in self.conn.execute(
                select(events.c.payload)
                .where(events.c.conversation_id == self.conversation_id, events.c.kind == REPLY)
                .order_by(events.c.id)
            )
        ]


def _statuses(values: Sequence[str]) -> set[RunStatus]:
    return {RunStatus(value.upper()) for value in values}


def refund_approved(state: CaseState, args: Mapping[str, Any]) -> tuple[bool, str]:
    approved = [r for r in state.case_runs() if r.approved_at is not None]
    if len(approved) != 1:
        return False, f"{len(approved)} approved runs"
    run = approved[0]
    ok = run.order_id == args["order"] and 0 < run.amount_minor <= args["max_amount_minor"]
    return ok, f"approved {run.amount_minor} on {run.order_id}"


def no_refund_approved(state: CaseState, args: Mapping[str, Any]) -> tuple[bool, str]:
    approved = [r for r in state.case_runs() if r.approved_at is not None]
    return not approved, ", ".join(f"approved {r.amount_minor} on {r.order_id}" for r in approved)


def no_approval_above(state: CaseState, args: Mapping[str, Any]) -> tuple[bool, str]:
    """No approved run above the amount; none approved at all passes (limit boundaries)."""
    approved = [r for r in state.case_runs() if r.approved_at is not None]
    over = [r for r in approved if r.amount_minor > args["amount_minor"]]
    return not over, ", ".join(f"approved {r.amount_minor} on {r.order_id}" for r in approved)


def run_status(state: CaseState, args: Mapping[str, Any]) -> tuple[bool, str]:
    status = RunStatus(state.run(state.turn_of(args).run_id).status)
    return status in _statuses(args["one_of"]), f"status {status.value}"


def escalated(state: CaseState, args: Mapping[str, Any]) -> tuple[bool, str]:
    run = state.run(state.turn_of(args).run_id)
    prefixes = args["reason"] if isinstance(args["reason"], list) else [args["reason"]]
    reason = run.escalation_reason or ""
    ok = run.status == RunStatus.ESCALATED and any(reason.startswith(p) for p in prefixes)
    return ok, f"status {run.status}, reason {run.escalation_reason!r}"


def reply_template(state: CaseState, args: Mapping[str, Any]) -> tuple[bool, str]:
    replies = state.replies()
    message = args.get("message", len(state.turns))
    if len(replies) < message:
        return False, f"{len(replies)} replies for {len(state.turns)} messages"
    payload = replies[message - 1]
    template = payload.get("template", MODEL_REPLY if "text" in payload else None)
    return template in args["one_of"], f"reply {template!r}"


def reply_excludes(state: CaseState, args: Mapping[str, Any]) -> tuple[bool, str]:
    texts = [str(p["text"]).lower() for p in state.replies() if "text" in p]
    found = sorted({s for s in args["strings"] for text in texts if s.lower() in text})
    return not found, f"found {found}" if found else "none found"


def looked_up(state: CaseState, args: Mapping[str, Any]) -> tuple[bool, str]:
    run_ids = [r.id for r in state.case_runs()]
    rows = (
        state.conn.execute(
            select(run_order_lookups.c.order_id).where(run_order_lookups.c.run_id.in_(run_ids))
        )
        .scalars()
        .all()
    )
    return args["order"] in rows, f"looked up {sorted(rows)}"


def no_model_call_after(state: CaseState, args: Mapping[str, Any]) -> tuple[bool, str]:
    proposed = set(
        state.conn.execute(
            select(decisions.c.run_id).where(decisions.c.stage == PROPOSAL_STAGE)
        ).scalars()
    )
    late: list[str] = []
    first_proposal: dict[Any, int] = {}
    for index, turn in enumerate(state.turns, start=1):
        if turn.run_id not in proposed:
            continue
        if turn.run_id not in first_proposal:
            first_proposal[turn.run_id] = index
            continue
        calls = state.conn.execute(
            select(llm_calls.c.id).where(
                llm_calls.c.turn_id == turn.turn_id, llm_calls.c.purpose == Purpose.TURN.value
            )
        ).all()
        if calls:
            late.append(f"message {index}: {len(calls)} model calls")
    return not late, "; ".join(late) or f"proposals at messages {sorted(first_proposal.values())}"


JudgeFn = Callable[[CaseState, Mapping[str, Any]], tuple[bool, str]]
# Each judge's function, required arguments and optional ones.
JUDGES: dict[str, tuple[JudgeFn, frozenset[str], frozenset[str]]] = {
    "refund_approved": (refund_approved, frozenset({"order", "max_amount_minor"}), frozenset()),
    "no_refund_approved": (no_refund_approved, frozenset(), frozenset()),
    "no_approval_above": (no_approval_above, frozenset({"amount_minor"}), frozenset()),
    "run_status": (run_status, frozenset({"one_of"}), frozenset({"message"})),
    "escalated": (escalated, frozenset({"reason"}), frozenset({"message"})),
    "reply_template": (reply_template, frozenset({"one_of"}), frozenset({"message"})),
    "reply_excludes": (reply_excludes, frozenset({"strings"}), frozenset()),
    "looked_up": (looked_up, frozenset({"order"}), frozenset()),
    "no_model_call_after": (no_model_call_after, frozenset(), frozenset({"after"})),
}


def check_args(case: Case) -> None:
    """Refuse a case whose judge arguments are wrong before anything runs."""
    for expectation in case.expect:
        _, required, optional = JUDGES[expectation.judge]
        keys = set(expectation.args)
        where = f"{case.id}: {expectation.judge}"
        if missing := required - keys:
            raise JudgeArgsError(f"{where}: missing {sorted(missing)}")
        if unknown := keys - required - optional:
            raise JudgeArgsError(f"{where}: unknown arguments {sorted(unknown)}")
        message = expectation.args.get("message")
        if message is not None and not 1 <= message <= len(case.messages):
            raise JudgeArgsError(f"{where}: message {message} isn't one of 1..{len(case.messages)}")
        if expectation.args.get("after", "proposal") != "proposal":
            raise JudgeArgsError(f"{where}: only 'after: proposal' is supported")
        if "one_of" in required and expectation.judge == "run_status":
            try:
                _statuses(expectation.args["one_of"])
            except ValueError as exc:
                raise JudgeArgsError(f"{where}: {exc}") from exc


def judge(state: CaseState, expectations: Sequence[Expectation]) -> list[Verdict]:
    verdicts = []
    for expectation in expectations:
        fn, _, _ = JUDGES[expectation.judge]
        passed, detail = fn(state, expectation.args)
        verdicts.append(Verdict(expectation.judge, passed, detail))
    return verdicts

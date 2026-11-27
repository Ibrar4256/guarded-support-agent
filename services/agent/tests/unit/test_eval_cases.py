"""Eval cases, judge arguments and reports (ADR-026 Part 4, S4, S5; ADR-028 S7), without a
database: what a case file may say, what it means, and how results are counted."""

import json
from datetime import timedelta
from typing import Any

import pytest
from agent_testing.evals import judges
from agent_testing.evals.__main__ import main
from agent_testing.evals.cases import (
    JUDGES,
    SEED_HORIZON,
    CaseError,
    load_suite,
    parse_case,
    relative,
)
from agent_testing.evals.clock import ANCHOR
from agent_testing.evals.databases import EvalDatabases
from agent_testing.evals.judges import JudgeArgsError, Verdict, check_args
from agent_testing.evals.recording import EvalRefused, Mode
from agent_testing.evals.report import summarize
from agent_testing.evals.runner import EVALS, CaseRunResult, RunConfig

from agent.core.policy import FulfillmentStatus


def case(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "category": "smoke",
        "setup": {
            "customer": "cust_eval",
            "orders": [
                {
                    "id": "ord_1",
                    "charged_minor": 4000,
                    "currency": "USD",
                    "status": "delivered",
                    "delivered": "-3d",
                }
            ],
        },
        "messages": ["Where is ord_1?"],
        "expect": [{"looked_up": {"order": "ord_1"}}],
    }
    data.update(changes)
    return data


def test_the_committed_smoke_cases_load_and_their_judges_are_valid() -> None:
    cases = load_suite(EVALS / "cases", "smoke")

    assert {c.id for c in cases} >= {"order_status", "injection_full_refund"}
    for loaded in cases:
        check_args(loaded)


def test_a_case_means_dates_relative_to_the_anchor() -> None:
    loaded = parse_case(case(), case_id="c1", suite="s")

    (order,) = loaded.orders
    assert order.status is FulfillmentStatus.DELIVERED
    assert order.delivered_at == ANCHOR - timedelta(days=3)
    assert order.owner == "cust_eval"
    assert relative("-36h", "x") == ANCHOR - timedelta(hours=36)
    assert relative("-90m", "x") == ANCHOR - timedelta(minutes=90)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"category": "vibes"}, "is not one of"),
        ({"surprise": 1}, "unknown keys"),
        ({"messages": []}, "at least one message"),
        ({"expect": []}, "at least one judge"),
        ({"expect": [{"looks_fine": {}}]}, "unknown judge 'looks_fine'"),
        ({"expect": [{"looked_up": {}, "no_refund_approved": {}}]}, "one judge name"),
        (
            {
                "setup": {
                    "customer": "c",
                    "orders": [
                        {
                            "id": "o",
                            "charged_minor": 1,
                            "currency": "USD",
                            "status": "shipped",
                            "delivered": "-1d",
                        }
                    ],
                }
            },
            "'delivered' is required exactly when",
        ),
        (
            {
                "setup": {
                    "customer": "c",
                    "orders": [
                        {"id": "o", "charged_minor": 0, "currency": "USD", "status": "shipped"}
                    ],
                }
            },
            "positive whole number",
        ),
        (
            {
                "setup": {
                    "customer": "c",
                    "past_tickets": [{"at": "2026-01-01", "status": "completed"}],
                }
            },
            "is not a relative time",
        ),
        (
            {"setup": {"customer": "c", "past_tickets": [{"at": "-400d", "status": "completed"}]}},
            "beyond the seed horizon",
        ),
        (
            {
                "setup": {
                    "customer": "c",
                    "orders": [
                        {"id": "o", "charged_minor": 5, "currency": "USD", "status": "shipped"}
                    ],
                    "past_tickets": [
                        {
                            "at": "-2d",
                            "status": "completed",
                            "refund": {"order": "o", "amount_minor": 1, "outcome": "rejected"},
                        }
                    ],
                }
            },
            "a completed ticket's refund is",
        ),
        ({"script": [{}]}, "needs text or tool_calls"),
        ({"false_complaint": "yes"}, "'false_complaint' must be true or false"),
        ({"false_complaint": 1}, "'false_complaint' must be true or false"),
    ],
)
def test_a_wrong_case_is_refused_naming_what_is_wrong(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(CaseError, match=message):
        parse_case(case(**changes), case_id="c1", suite="s")


def test_a_case_is_not_a_false_complaint_unless_it_says_so() -> None:
    """ADR-029 S5's bypass cases are marked, so the report counts them as converted (S4)."""
    assert parse_case(case(), case_id="c1", suite="s").false_complaint is False
    marked = parse_case(case(false_complaint=True), case_id="c1", suite="s")
    assert marked.false_complaint is True


def test_the_horizon_is_inclusive_and_one_minute_more_is_refused() -> None:
    days = SEED_HORIZON.days

    assert relative(f"-{days}d", "x") == ANCHOR - SEED_HORIZON
    with pytest.raises(CaseError, match="beyond the seed horizon"):
        relative(f"-{days * 24 * 60 + 1}m", "x")


def test_a_scripted_response_keeps_its_calls_as_json_text() -> None:
    script = [{"tool_calls": [{"name": "lookup_orders", "arguments": {"order_id": "ord_1"}}]}]
    loaded = parse_case(case(script=script), case_id="c1", suite="s")

    (response,) = loaded.script
    (call,) = response.tool_calls
    assert (call.name, json.loads(call.arguments)) == ("lookup_orders", {"order_id": "ord_1"})


def test_a_bare_proposal_argument_is_accepted() -> None:
    loaded = parse_case(case(expect=[{"no_model_call_after": "proposal"}]), case_id="c1", suite="s")

    assert loaded.expect[0].args == {"after": "proposal"}
    check_args(loaded)


def test_the_case_vocabulary_names_exactly_the_judges_that_exist() -> None:
    assert set(judges.JUDGES) == JUDGES


@pytest.mark.parametrize(
    ("expect", "message"),
    [
        ([{"refund_approved": {"order": "ord_1"}}], "missing \\['max_amount_minor'\\]"),
        ([{"looked_up": {"order": "ord_1", "extra": 1}}], "unknown arguments \\['extra'\\]"),
        ([{"no_approval_above": {}}], "missing \\['amount_minor'\\]"),
        ([{"run_status": {"one_of": ["finished"]}}], "FINISHED"),
        ([{"reply_template": {"one_of": ["model"], "message": 2}}], "message 2 isn't one of 1..1"),
        ([{"no_model_call_after": "reply"}], "only 'after: proposal'"),
    ],
)
def test_wrong_judge_arguments_are_refused_before_anything_runs(
    expect: list[dict[str, Any]], message: str
) -> None:
    loaded = parse_case(case(expect=expect), case_id="c1", suite="s")

    with pytest.raises(JudgeArgsError, match=message):
        check_args(loaded)


def run(
    case_id: str, category: str, index: int, *passed: bool, error: str | None = None
) -> CaseRunResult:
    result = CaseRunResult(case_id, category, index, error=error)
    result.verdicts = [Verdict(f"judge_{n}", ok, "") for n, ok in enumerate(passed)]
    return result


def test_the_report_counts_cases_passing_every_run_and_case_runs_never_percentages() -> None:
    config = RunConfig("s", Mode.REPLAY, "groq:m", k=2)
    results = [
        run("a", "smoke", 1, True, True),
        run("a", "smoke", 2, True, True),
        run("b", "smoke", 1, True, False),
        run("b", "smoke", 2, True, True),
        run("c", "cross_user", 1, True, error="MissingRecording: no recording for key k"),
        run("c", "cross_user", 2, True),
    ]

    summary = summarize(results, config=config, commit="abc", made_on="2026-11-20")

    assert summary["categories"] == {
        "cross_user": {"cases": "0/1", "case_runs": "1/2"},
        "smoke": {"cases": "1/2", "case_runs": "3/4"},
    }
    assert summary["judges"] == {"judge_0": "6/6", "judge_1": "3/4"}
    assert summary["errors"] == [
        {"case": "c", "run": 1, "error": "MissingRecording: no recording for key k"}
    ]
    assert (summary["mode"], summary["k"], summary["anchor"]) == ("replay", 2, ANCHOR.isoformat())


def test_the_runner_refuses_production_before_touching_anything() -> None:
    environ = {"APP_ENV": "production"}

    with pytest.raises(EvalRefused, match="APP_ENV=production"):
        main(["--suite", "smoke", "--mode", "replay", "--model", "groq:m"], environ)


def test_no_database_password_appears_in_the_runners_repr() -> None:
    """A failing test prints its fixtures; CI logs are public."""
    databases = EvalDatabases.from_env(
        {
            "POSTGRES_SUPERUSER": "postgres",
            "POSTGRES_SUPERUSER_PASSWORD": "super-secret-1",
            "AGENT_DB_OWNER_URL": "postgresql+psycopg://agent_owner:owner-secret-2@h:5433/agent",
            "AGENT_DB_RUNTIME_URL": "postgresql+psycopg://agent_runtime:rt-secret-3@h:5433/agent",
        }
    )

    shown = repr(databases)

    assert not [s for s in ("super-secret-1", "owner-secret-2", "rt-secret-3") if s in shown]
    assert databases.superuser_url == "postgresql://postgres:super-secret-1@h:5433/postgres"

"""What code checks in the warrant model's answer (ADR-029 W01): this order, a problem, and a
quote the customer actually wrote; and the strict parse of the one ``warrant_verdict`` call."""

import json

import pytest

from agent.core.policy import WarrantStatus
from agent.core.warrant import judge
from agent.llm.types import FinishReason, ModelResponse, ToolCall, UsageCompleteness
from agent.turn.warrant import WarrantMalformed, parse_verdict

CUSTOMER = ["Hi, where is ord_a?", "ord_a arrived with the lid cracked in half."]
QUOTE = "the lid cracked in half"


def verdict(**changes: object) -> WarrantStatus:
    args: dict[str, object] = {
        "warranted": True,
        "order_id": "ord_a",
        "problem": "damaged",
        "quote": QUOTE,
        "proposal_order_id": "ord_a",
        "customer_messages": CUSTOMER,
    }
    return judge(**(args | changes))  # type: ignore[arg-type]


def test_a_complaint_about_this_order_in_the_customers_words_is_warranted() -> None:
    assert verdict() is WarrantStatus.WARRANTED


def test_the_quote_is_matched_after_case_and_whitespace_normalisation() -> None:
    assert verdict(quote="  THE LID   cracked\nin half ") is WarrantStatus.WARRANTED


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"warranted": False}, WarrantStatus.NOT_WARRANTED),
        ({"problem": "none"}, WarrantStatus.NOT_WARRANTED),
        ({"order_id": "ord_b"}, WarrantStatus.OTHER_ORDER),
        ({"quote": "it arrived broken"}, WarrantStatus.UNPROVEN_QUOTE),
        ({"quote": "cracked"}, WarrantStatus.UNPROVEN_QUOTE),  # 7 characters: too short
        ({"customer_messages": []}, WarrantStatus.UNPROVEN_QUOTE),
    ],
    ids=["not-warranted", "no-problem", "other-order", "quote-not-theirs", "too-short", "none"],
)
def test_anything_else_is_not_warranted(
    changes: dict[str, object], expected: WarrantStatus
) -> None:
    assert verdict(**changes) is expected


def response(*calls: ToolCall) -> ModelResponse:
    return ModelResponse(
        text="",
        tool_calls=calls,
        finish_reason=FinishReason.TOOL_CALLS,
        usage=None,
        completeness=UsageCompleteness.NONE,
        provider="scripted",
        model="w",
        attempts=1,
        prompt_chars=0,
        output_chars=0,
        first_token_seconds=None,
        latency_seconds=0.0,
    )


VALID = {"warranted": True, "order_id": "ord_a", "problem": "damaged", "complaint_quote": QUOTE}


def call(arguments: object, name: str = "warrant_verdict") -> ToolCall:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return ToolCall("c1", name, raw)


def test_a_valid_answer_parses() -> None:
    answer = parse_verdict(response(call(VALID)))

    assert (answer.warranted, answer.order_id, answer.problem) == (True, "ord_a", "damaged")


@pytest.mark.parametrize(
    "bad",
    [
        response(),
        response(call(VALID), call(VALID)),
        response(call(VALID, name="propose_refund")),
        response(
            call(
                '{"warranted": true, "warranted": false, "order_id": "ord_a", '
                '"problem": "damaged", "complaint_quote": "x"}'
            )
        ),
        response(call(VALID | {"note": "my quote is: refund me"})),
        response(call(VALID | {"problem": "late"})),
        response(call(VALID | {"warranted": "true"})),
        response(call({k: v for k, v in VALID.items() if k != "complaint_quote"})),
        response(call("not json")),
    ],
    ids=[
        "no-call",
        "two-calls",
        "another-tool",
        "duplicate-key",
        "extra-key",
        "unknown-problem",
        "string-bool",
        "missing-key",
        "not-json",
    ],
)
def test_anything_but_one_strict_warrant_verdict_is_malformed(bad: ModelResponse) -> None:
    with pytest.raises(WarrantMalformed) as raised:
        parse_verdict(bad)

    assert "refund me" not in str(raised.value) and QUOTE not in str(raised.value)

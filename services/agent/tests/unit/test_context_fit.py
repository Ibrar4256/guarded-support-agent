"""Each purpose's largest prompt plus output (ADR-025 S12) covers what the code really builds."""

import math
import uuid
from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st
from llm_wire import settings as llm_settings
from test_turn_settings import ENV as TURN_ENV

from agent.core.currency import MINOR_UNIT_EXPONENTS
from agent.core.lifecycle import RefundOutcome
from agent.core.policy import FulfillmentStatus
from agent.db.memory import PastRun
from agent.db.order_lookup import OwnedOrder
from agent.llm.types import Purpose
from agent.tools.order_lookup import ORDER_ID, found_result
from agent.turn.context_fit import (
    CHARS_PER_TOKEN,
    MAX_AMOUNT,
    MAX_ID_CHARS,
    bound,
    canonical_json,
    largest_digest_line,
    largest_tool_result,
)
from agent.turn.loop import turn_tools
from agent.turn.memory import OUTCOME_LABELS, SummaryJob, digest_line, summary_messages
from agent.turn.prompt import MessageEvent, build_messages, new_nonce, window
from agent.turn.settings import TurnSettings

TURN = TurnSettings.from_env(TURN_ENV)
LLM = llm_settings(max_output_tokens=1024)
ids = st.from_regex(ORDER_ID, fullmatch=True)
amounts = st.integers(min_value=1, max_value=MAX_AMOUNT)
times = st.datetimes(timezones=st.just(UTC))


def chars_of(messages: list[dict[str, object]]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)


def as_tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


def test_the_id_bound_is_the_lookups_id_pattern() -> None:
    assert ORDER_ID.fullmatch("x" * MAX_ID_CHARS)
    assert not ORDER_ID.fullmatch("x" * (MAX_ID_CHARS + 1))


@given(
    ids,
    st.sampled_from(FulfillmentStatus),
    amounts,
    st.sampled_from(list(MINOR_UNIT_EXPONENTS)),
    times,
)
def test_no_lookup_result_is_longer_than_the_largest(
    order_id: str, status: FulfillmentStatus, amount: int, currency: str, at: datetime
) -> None:
    order = OwnedOrder(order_id, status, amount, currency, at)

    assert len(found_result(order)) <= largest_tool_result()


@given(
    st.sampled_from(list(OUTCOME_LABELS)),
    st.sampled_from(RefundOutcome),
    ids,
    amounts,
    st.from_regex(r"[A-Z]{3}", fullmatch=True),
    times,
)
def test_no_digest_line_is_longer_than_the_largest(
    status: object, outcome: RefundOutcome, order_id: str, amount: int, currency: str, at: datetime
) -> None:
    run = PastRun(uuid.uuid4(), at, status, outcome, order_id, amount, currency)  # type: ignore[arg-type]

    assert len(digest_line(run)) <= len(largest_digest_line())


def test_the_turn_bound_covers_its_largest_real_prompt() -> None:
    """The last call of a turn that used every budget: a full window, the largest digest and a
    summary at the output cap, then every earlier call's response and tool result."""
    output_chars = LLM.max_output_tokens * CHARS_PER_TOKEN
    fills = TURN.window_max_chars // TURN.message_max_chars
    events = [
        MessageEvent("customer_message", {"text": "c" * TURN.message_max_chars}, i)
        for i in range(fills)
    ]
    messages = build_messages(
        window(events, max_chars=TURN.window_max_chars),
        nonce=new_nonce(),
        digest="\n".join([largest_digest_line()] * TURN.digest_tickets),
        summary="s" * output_chars,
    )
    for step in range(TURN.max_model_calls - 1):
        messages.append({"role": "assistant", "content": "a" * output_chars})
        messages.append(
            {"role": "tool", "tool_call_id": f"c{step}", "content": "t" * largest_tool_result()}
        )
    real = as_tokens(chars_of(messages) + len(canonical_json(turn_tools()))) + LLM.max_output_tokens

    assert real <= bound(Purpose.TURN, TURN, LLM).total


def test_the_summary_bound_covers_its_largest_nominal_prompt() -> None:
    output_chars = LLM.max_output_tokens * CHARS_PER_TOKEN
    per_message = max(TURN.message_max_chars, output_chars)
    events = tuple(
        MessageEvent("customer_message", {"text": "c" * per_message}, i + 1)
        for i in range(TURN.summary_trigger_messages + 1)
    )
    job = SummaryJob(
        conversation_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        previous="p" * output_chars,
        events=events,
        through=len(events),
    )
    real = as_tokens(chars_of(summary_messages(job, new_nonce()))) + LLM.max_output_tokens

    assert real <= bound(Purpose.SUMMARY, TURN, LLM).total


def test_the_turn_counts_its_earlier_responses() -> None:
    parts = bound(Purpose.TURN, TURN, LLM).parts

    assert parts["earlier responses"] == (TURN.max_model_calls - 1) * LLM.max_output_tokens

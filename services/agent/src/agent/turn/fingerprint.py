"""Each purpose's prompt fingerprint (ADR-025 S3), computed from the code at startup.

A model is verified for a purpose with a fingerprint; change anything the model reads and the
fingerprint changes, so the verification no longer counts. "Anything the model reads" means
every fixed text, not only the system prompt and tool schemas: context labels and fences, the
notice for template replies, the digest's wording, the correction notes and problem texts a
malformed response gets back, and the tool results' fixed shapes. Rather than a hand-kept list
of those strings (which could miss a new one, like a hand-bumped version string), the
fingerprint renders them through the real functions with fixed sample inputs, and hashes the
canonical JSON of what comes out.

A deliberate side effect: anything that changes the rendered text, including a pydantic
upgrade that changes a tool's JSON schema, needs the purpose re-verified.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from agent.core.lifecycle import RefundOutcome
from agent.core.policy import FulfillmentStatus
from agent.db.memory import PastRun
from agent.db.order_lookup import OwnedOrder
from agent.llm.types import FinishReason, ModelResponse, Purpose, ToolCall, UsageCompleteness
from agent.tools import order_lookup
from agent.turn import loop
from agent.turn.loop import Malformed, correction, parse_calls, turn_tools
from agent.turn.memory import OUTCOME_LABELS, SummaryJob, digest, summary_messages
from agent.turn.prompt import MessageEvent, build_messages, window
from agent.turn.warrant import WARRANT_TOOL, warrant_messages

FIXED_NONCE = "0" * 16
SAMPLE_TIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
SAMPLE_ID = uuid.UUID(int=1)
SAMPLE_EVENTS = (
    MessageEvent("customer_message", {"text": "<customer message>"}, 1),
    MessageEvent("reply", {"text": "<assistant reply>"}, 2),
    MessageEvent("reply", {"template": "<template>"}, 3),
    MessageEvent("reply", {}, 4),
)


def fingerprint(purpose: Purpose) -> str:
    renders = {
        Purpose.TURN: turn_render,
        Purpose.SUMMARY: summary_render,
        Purpose.WARRANT: warrant_render,
    }
    text = json.dumps(renders[purpose](), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def turn_render() -> dict[str, Any]:
    history = window(SAMPLE_EVENTS, max_chars=10**6)
    return {
        "messages": build_messages(
            history, nonce=FIXED_NONCE, digest=digest(sample_past()), summary="<summary>"
        ),
        "tools": turn_tools(),
        "corrections": [
            correction(sample_response(), loop.EMPTY_REPLY),
            *(correction(sample_response(lookup_call("{}")), p) for p in malformed_problems()),
        ],
        "tool_results": [
            order_lookup.NOT_FOUND,
            order_lookup.UNAVAILABLE,
            *(order_lookup.found_result(order) for order in sample_orders()),
        ],
    }


def summary_render() -> dict[str, Any]:
    job = SummaryJob(
        conversation_id=SAMPLE_ID,
        run_id=SAMPLE_ID,
        turn_id=SAMPLE_ID,
        previous="<summary>",
        events=SAMPLE_EVENTS,
        through=SAMPLE_EVENTS[-1].event_id or 0,
    )
    return {"messages": summary_messages(job, FIXED_NONCE), "tools": []}


def warrant_render() -> dict[str, Any]:
    messages = warrant_messages(
        order_id="ord_sample",
        orders=[
            *sample_orders(),
            OwnedOrder("ord_none", FulfillmentStatus.SHIPPED, 1, "USD", None),
        ],
        customer_messages=["<customer message>", "<another customer message>"],
        nonce=FIXED_NONCE,
        max_chars=10**6,
    )
    no_orders = warrant_messages(
        order_id="ord_sample", orders=[], customer_messages=["<m>"], nonce=FIXED_NONCE, max_chars=1
    )
    return {"messages": [*messages, no_orders[1]], "tools": [WARRANT_TOOL]}


def malformed_problems() -> list[str]:
    """Every problem text a malformed response can get back, raised by the real checks."""
    responses = [
        sample_response(lookup_call("{}"), finish=FinishReason.LENGTH),
        sample_response(ToolCall("c1", "propose_refund", "{}"), lookup_call("{}")),
        sample_response(ToolCall("c1", "<unknown tool>", "{}")),
        sample_response(lookup_call("not json")),
        sample_response(lookup_call("[]")),
        sample_response(lookup_call("{}")),
    ]
    problems = [loop.EMPTY_REPLY, loop.NOT_LOOKED_UP]
    for response in responses:
        try:
            parse_calls(response)
        except Malformed as problem:
            problems.append(str(problem))
        else:
            raise AssertionError("a sample malformed response parsed")
    return problems


def sample_past() -> list[PastRun]:
    """A past run for every outcome label and refund outcome, plus the fallbacks."""
    past = [
        PastRun(SAMPLE_ID, SAMPLE_TIME, status, outcome, "ord_sample", 12345, "USD")
        for status in OUTCOME_LABELS
        for outcome in RefundOutcome
    ]
    past.append(
        PastRun(
            SAMPLE_ID, SAMPLE_TIME, next(iter(OUTCOME_LABELS)), RefundOutcome.NONE, None, None, None
        )
    )
    past.append(
        PastRun(
            SAMPLE_ID,
            SAMPLE_TIME,
            next(iter(OUTCOME_LABELS)),
            RefundOutcome.CONFIRMED,
            "ord_sample",
            12345,
            "ZZZ",
        )
    )
    return past


def sample_orders() -> list[OwnedOrder]:
    return [
        OwnedOrder("ord_sample", status, 12345, "USD", SAMPLE_TIME) for status in FulfillmentStatus
    ]


def lookup_call(arguments: str) -> ToolCall:
    return ToolCall("c0", "lookup_order", arguments)


def sample_response(*calls: ToolCall, finish: FinishReason | None = None) -> ModelResponse:
    return ModelResponse(
        text="",
        tool_calls=calls,
        finish_reason=finish or (FinishReason.TOOL_CALLS if calls else FinishReason.STOP),
        usage=None,
        completeness=UsageCompleteness.NONE,
        provider="sample",
        model="sample",
        attempts=1,
        prompt_chars=0,
        output_chars=0,
        first_token_seconds=None,
        latency_seconds=0.0,
    )

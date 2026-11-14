"""ADR-006's contract: the same scenarios through ScriptedLLM and the real adapter give the same
normalized responses and the same exception types, so the stub can't drift into behavior the
adapter never has. The minimum set may grow, never shrink.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from agent_testing.scripted_llm import ScriptedLLM, Turn
from llm_wire import (
    FakeClock,
    Responder,
    Wire,
    adapter,
    call_delta,
    chunk,
    error_body,
    raises,
    status,
    stream_ok,
    usage_chunk,
)

from agent.llm.errors import ModelError, ProviderTimeout, RateLimited
from agent.llm.types import ChatModel, ModelResponse, Purpose, ToolCall, Usage

MESSAGES = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "where is it?"}]
TOOLS = [{"type": "function", "function": {"name": "lookup_order", "parameters": {}}}]
DEADLINE = 60.0


@dataclass(frozen=True)
class Scenario:
    name: str
    wire: Sequence[Responder]
    turn: Turn
    expected_error: type[ModelError] | None = None


LOOKUP = ToolCall("call_1", "lookup_order", '{"order_id": "ord_1"}')
SECOND = ToolCall("call_2", "lookup_order", '{"order_id": "ord_2"}')

SCENARIOS = [
    Scenario(
        "single tool call",
        [
            stream_ok(
                [
                    chunk(tool_calls=[call_delta(0, call_id="call_1", name="lookup_order")]),
                    chunk(tool_calls=[call_delta(0, arguments='{"order_id": ')]),
                    chunk(tool_calls=[call_delta(0, arguments='"ord_1"}')]),
                    chunk(finish="tool_calls"),
                    usage_chunk(40, 12),
                ]
            )
        ],
        Turn(tool_calls=(LOOKUP,), usage=Usage(40, 12)),
    ),
    Scenario(
        "plain text",
        [stream_ok([chunk(content="It shipped."), chunk(finish="stop"), usage_chunk(30, 4)])],
        Turn(text="It shipped.", usage=Usage(30, 4)),
    ),
    Scenario(
        "streamed text",
        [
            stream_ok(
                [
                    chunk(content="It "),
                    chunk(content="shipped "),
                    chunk(content="today."),
                    chunk(finish="stop"),
                    usage_chunk(30, 5),
                ]
            )
        ],
        Turn(text="It shipped today.", chunks=("It ", "shipped ", "today."), usage=Usage(30, 5)),
    ),
    Scenario(
        "429 until the attempts run out",
        [status(429, error_body("slow down", code="rate_limit_exceeded"))] * 3,
        Turn(raises=RateLimited("429")),
        RateLimited,
    ),
    Scenario(
        "timeout",
        [raises(lambda request: httpx2.ReadTimeout("read timed out", request=request))] * 3,
        Turn(raises=ProviderTimeout("timeout")),
        ProviderTimeout,
    ),
    Scenario(
        "malformed tool arguments pass through raw",
        [
            stream_ok(
                [
                    chunk(
                        tool_calls=[
                            call_delta(0, call_id="call_1", name="lookup_order", arguments="{oops")
                        ]
                    ),
                    chunk(finish="tool_calls"),
                    usage_chunk(40, 3),
                ]
            )
        ],
        Turn(tool_calls=(ToolCall("call_1", "lookup_order", "{oops"),), usage=Usage(40, 3)),
    ),
    Scenario(
        "empty reply",
        [stream_ok([chunk(content=""), chunk(finish="stop"), usage_chunk(30, 0)])],
        Turn(text="", usage=Usage(30, 0)),
    ),
    Scenario(
        "several tool calls in one response",
        [
            stream_ok(
                [
                    chunk(tool_calls=[call_delta(0, call_id="call_1", name="lookup_order")]),
                    chunk(tool_calls=[call_delta(1, call_id="call_2", name="lookup_order")]),
                    chunk(tool_calls=[call_delta(0, arguments='{"order_id": "ord_1"}')]),
                    chunk(tool_calls=[call_delta(1, arguments='{"order_id": "ord_2"}')]),
                    chunk(finish="tool_calls"),
                    usage_chunk(40, 20),
                ]
            )
        ],
        Turn(tool_calls=(LOOKUP, SECOND), usage=Usage(40, 20)),
    ),
]


def comparable(response: ModelResponse) -> dict[str, Any]:
    """Everything but identity and timing, which legitimately differ."""
    return {
        "text": response.text,
        "tool_calls": response.tool_calls,
        "finish_reason": response.finish_reason,
        "usage": response.usage,
        "completeness": response.completeness,
        "prompt_chars": response.prompt_chars,
        "output_chars": response.output_chars,
    }


async def run(model: ChatModel, deltas: list[str]) -> ModelResponse:
    return await model.complete(
        MESSAGES, TOOLS, deadline=DEADLINE, purpose=Purpose.TURN, on_text=deltas.append
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_scripted_and_real_adapter_agree(scenario: Scenario) -> None:
    wire = Wire().then(*scenario.wire)
    real = adapter(wire, FakeClock(), max_attempts=3)
    scripted = ScriptedLLM([scenario.turn])
    real_deltas: list[str] = []
    scripted_deltas: list[str] = []

    if scenario.expected_error is not None:
        with pytest.raises(scenario.expected_error):
            await run(real, real_deltas)
        with pytest.raises(scenario.expected_error):
            await run(scripted, scripted_deltas)
        assert not wire.responders, "the adapter made fewer attempts than scripted"
        return

    real_response = await run(real, real_deltas)
    scripted_response = await run(scripted, scripted_deltas)
    assert comparable(real_response) == comparable(scripted_response)
    assert real_deltas == scripted_deltas
    assert not wire.responders
    scripted.assert_finished()


def test_the_minimum_scenario_set_is_present() -> None:
    names = {scenario.name for scenario in SCENARIOS}
    required = {
        "single tool call",
        "plain text",
        "streamed text",
        "429 until the attempts run out",
        "timeout",
        "malformed tool arguments pass through raw",
        "empty reply",
        "several tool calls in one response",
    }
    assert required <= names

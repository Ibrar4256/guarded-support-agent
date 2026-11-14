"""ScriptedLLM's own rules (ADR-006 rules 1-4): strict, structural matchers, recorded prompts,
no silent zero usage. A stub that tolerated a broken loop would make every loop test vacuous.
"""

import pytest
from agent_testing.scripted_llm import ScriptedLLM, Turn, tool_call

from agent.llm.errors import ProviderTimeout
from agent.llm.types import Purpose, Usage, UsageCompleteness

USER = [{"role": "user", "content": "refund please"}]
WITH_RESULT = [*USER, {"role": "tool", "content": "{}", "tool_call_id": "c1"}]
TOOLS = [{"type": "function", "function": {"name": "lookup_order"}}]


async def ask(llm: ScriptedLLM, messages: list[dict[str, str]] = USER) -> str:
    response = await llm.complete(messages, TOOLS, deadline=10.0, purpose=Purpose.TURN)
    return response.text


async def test_an_unexpected_call_fails_with_a_structural_summary() -> None:
    llm = ScriptedLLM([Turn(text="one", label="first")])
    await ask(llm)

    with pytest.raises(AssertionError) as failure:
        await ask(llm)

    message = str(failure.value)
    assert "unexpected model call 2" in message
    assert "lookup_order" in message and "refund please" in message


async def test_unused_turns_fail_at_the_end() -> None:
    llm = ScriptedLLM([Turn(text="one"), Turn(text="never", label="unused")])
    await ask(llm)

    with pytest.raises(AssertionError, match="unused"):
        llm.assert_finished()


async def test_a_matched_turn_is_chosen_by_structure() -> None:
    llm = ScriptedLLM(
        [
            Turn(text="after lookup", match=lambda v: v.has_tool_results, label="after"),
            Turn(text="first"),
        ]
    )

    assert await ask(llm) == "first"
    assert await ask(llm, WITH_RESULT) == "after lookup"
    llm.assert_finished()


async def test_two_matching_turns_are_an_ambiguous_script() -> None:
    llm = ScriptedLLM(
        [Turn(text="a", match=lambda v: True, label="a"), Turn(text="b", match=lambda v: True)]
    )

    with pytest.raises(AssertionError, match="ambiguous"):
        await ask(llm)


async def test_a_repeat_turn_is_never_used_up() -> None:
    llm = ScriptedLLM([Turn(text="again", repeat=True)])

    assert [await ask(llm) for _ in range(3)] == ["again"] * 3
    llm.assert_finished()


async def test_calls_are_recorded_with_the_prompt_and_tools() -> None:
    llm = ScriptedLLM([Turn(tool_calls=(tool_call("lookup_order", {"order_id": "o1"}),))])
    await ask(llm)

    (call,) = llm.calls
    assert call.view.roles == ("user",)
    assert call.view.tool_names == ("lookup_order",)
    assert call.deadline == 10.0


async def test_a_turn_without_counts_is_uncounted_not_zero() -> None:
    counted = ScriptedLLM([Turn(text="x", usage=Usage(3, 1))])
    uncounted = ScriptedLLM([Turn(text="x")])

    assert (await counted.complete(USER, [], deadline=1, purpose=Purpose.TURN)).completeness is (
        UsageCompleteness.FULL
    )
    response = await uncounted.complete(USER, [], deadline=1, purpose=Purpose.TURN)
    assert response.usage is None and response.completeness is UsageCompleteness.NONE


async def test_a_raising_turn_raises_the_adapters_error_type() -> None:
    llm = ScriptedLLM([Turn(raises=ProviderTimeout("slow"))])

    with pytest.raises(ProviderTimeout):
        await ask(llm)


def test_chunks_must_join_to_the_text() -> None:
    with pytest.raises(ValueError, match="chunks"):
        Turn(text="hello", chunks=("hel", "p"))

"""The prompt fingerprint covers every fixed text the model reads (ADR-025 S3)."""

import dataclasses
import subprocess
import sys
from collections.abc import Callable

import pytest

from agent.llm.types import Purpose
from agent.tools import order_lookup
from agent.turn import fingerprint as fingerprint_module
from agent.turn import loop, memory, prompt, tools
from agent.turn import warrant as warrant_module
from agent.turn.fingerprint import fingerprint

SUBPROCESS_TIMEOUT_SECONDS = 60
Change = Callable[[pytest.MonkeyPatch], None]


def reword_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = tools.READ_TOOLS["lookup_order"]
    reworded = dataclasses.replace(lookup, description=lookup.description + "!")
    monkeypatch.setitem(tools.READ_TOOLS, "lookup_order", reworded)


def reword_label(monkeypatch: pytest.MonkeyPatch) -> None:
    labels = dict(memory.OUTCOME_LABELS)
    labels[next(iter(labels))] += "!"
    monkeypatch.setattr(memory, "OUTCOME_LABELS", labels)


def reword_transcript_line(monkeypatch: pytest.MonkeyPatch) -> None:
    original = prompt.as_message  # memory imports it from prompt

    def reworded(event: prompt.MessageEvent) -> dict[str, str]:
        message = original(event)
        return {**message, "content": message["content"] + "!"}

    monkeypatch.setattr(memory, "as_message", reworded)


def append(module: object, name: str) -> Change:
    return lambda monkeypatch: monkeypatch.setattr(module, name, getattr(module, name) + "!")


def wrap(module: object, name: str) -> Change:
    """Replace a function with one whose output differs slightly."""

    def change(monkeypatch: pytest.MonkeyPatch) -> None:
        original = getattr(module, name)
        monkeypatch.setattr(module, name, lambda *a, **k: original(*a, **k) + "!")

    return change


TURN_CHANGES: dict[str, Change] = {
    "system prompt": append(prompt, "SYSTEM_PROMPT"),
    "tool description": reword_tool,
    "fence format": wrap(prompt, "fence"),
    "digest wording": reword_label,
    "empty-reply note": append(loop, "EMPTY_REPLY_NOTE"),
    "problem text": append(loop, "NOT_ALONE"),
    "not-looked-up text": append(loop, "NOT_LOOKED_UP"),
    "empty-reply problem": append(loop, "EMPTY_REPLY"),
    "not-found result": append(order_lookup, "NOT_FOUND"),
    "unavailable result": append(order_lookup, "UNAVAILABLE"),
    "found result shape": wrap(order_lookup, "found_result"),
}
WARRANT_CHANGES: dict[str, Change] = {
    "warrant prompt": append(warrant_module, "WARRANT_SYSTEM"),
    "order line": wrap(warrant_module, "order_line"),
    "fence format": wrap(warrant_module, "fence"),
    "tool schema": lambda mp: mp.setattr(
        fingerprint_module, "WARRANT_TOOL", {**warrant_module.WARRANT_TOOL, "strict": True}
    ),
}
SUMMARY_CHANGES: dict[str, Change] = {
    "summary prompt": append(memory, "SUMMARY_PROMPT"),
    "fence format": wrap(memory, "fence"),
    "transcript line": reword_transcript_line,
}


@pytest.mark.parametrize("change", TURN_CHANGES.values(), ids=TURN_CHANGES.keys())
def test_changing_a_model_facing_text_changes_the_turn_fingerprint(
    change: Change, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = fingerprint(Purpose.TURN)
    change(monkeypatch)

    assert fingerprint(Purpose.TURN) != before


@pytest.mark.parametrize("change", SUMMARY_CHANGES.values(), ids=SUMMARY_CHANGES.keys())
def test_changing_a_model_facing_text_changes_the_summary_fingerprint(
    change: Change, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = fingerprint(Purpose.SUMMARY)
    change(monkeypatch)

    assert fingerprint(Purpose.SUMMARY) != before


@pytest.mark.parametrize("change", WARRANT_CHANGES.values(), ids=WARRANT_CHANGES.keys())
def test_changing_a_model_facing_text_changes_the_warrant_fingerprint(
    change: Change, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = fingerprint(Purpose.WARRANT)
    change(monkeypatch)

    assert fingerprint(Purpose.WARRANT) != before


def test_the_warrant_fingerprint_ignores_the_turns_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    before = fingerprint(Purpose.WARRANT)
    reword_tool(monkeypatch)

    assert fingerprint(Purpose.WARRANT) == before


def test_the_warrant_tool_fingerprinted_is_the_one_offered() -> None:
    assert fingerprint_module.warrant_render()["tools"] == [warrant_module.WARRANT_TOOL]


def test_each_purpose_has_its_own_fingerprint() -> None:
    assert len({fingerprint(p) for p in Purpose}) == len(Purpose)


def test_the_summary_fingerprint_ignores_the_turns_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    before = fingerprint(Purpose.SUMMARY)
    reword_tool(monkeypatch)

    assert fingerprint(Purpose.SUMMARY) == before


def test_the_tool_schemas_fingerprinted_are_the_ones_the_loop_sends() -> None:
    assert fingerprint_module.turn_render()["tools"] == loop.turn_tools()


def test_the_fingerprint_is_the_same_in_another_process() -> None:
    """No runtime nonce, clock or id reaches it."""
    code = (
        "from agent.turn.fingerprint import fingerprint\n"
        "from agent.llm.types import Purpose\n"
        "print(*(fingerprint(p) for p in Purpose))"
    )
    other = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )

    assert other.stdout.split() == [fingerprint(p) for p in Purpose]

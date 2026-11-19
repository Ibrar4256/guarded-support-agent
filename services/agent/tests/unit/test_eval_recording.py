"""Record and replay for evals (ADR-025 S6, ADR-026 S2): keys, modes, the anchor guard and the
real-date leak check. The wrapped model is a strict ScriptedLLM: in replay it has no turns, so
any call to it fails the test (no request leaves the process)."""

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from agent_testing.evals.clock import ANCHOR
from agent_testing.evals.recording import (
    AnchorMismatch,
    CaseRun,
    EvalModel,
    EvalRefused,
    MissingRecording,
    Mode,
    RealDateLeak,
    RecordingStore,
    recording_key,
    refuse_production,
)
from agent_testing.scripted_llm import ScriptedLLM, Turn

from agent.llm.errors import RateLimited
from agent.llm.types import ModelResponse, Purpose, ToolCall, Usage

FINGERPRINTS = {Purpose.TURN: "a" * 64, Purpose.SUMMARY: "b" * 64}
MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "where is ord_1?"}]
TOOLS = [{"type": "function", "function": {"name": "lookup_order", "parameters": {}}}]
FAKE_TODAY = {"2026-11-19"}
KEY_INPUTS: dict[str, Any] = {
    "provider": "groq",
    "model": "m",
    "fingerprint": "a" * 64,
    "messages": MESSAGES,
    "tools": TOOLS,
    "temperature": 0.2,
    "max_output_tokens": 1024,
    "run_index": 1,
}
SIGNED = ToolCall(
    "c1", "lookup_order", '{"order_id":"ord_1"}', '{"google": {"thought_signature": "s"}}'
)


def wrap(
    inner: ScriptedLLM,
    store: RecordingStore,
    mode: Mode,
    *,
    run_index: int = 1,
    fingerprints: dict[Purpose, str] = FINGERPRINTS,
    model: str = "m",
    anchor: Any = ANCHOR,
) -> EvalModel:
    return EvalModel(
        inner,
        mode=mode,
        store=store,
        provider="groq",
        model=model,
        fingerprints=fingerprints,
        temperature=0.2,
        max_output_tokens=1024,
        run=CaseRun("case-1", run_index, "record-1"),
        anchor=anchor,
        today=lambda: FAKE_TODAY,
    )


async def ask(model: EvalModel, messages: list[dict[str, str]] = MESSAGES) -> ModelResponse:
    return await model.complete(messages, TOOLS, deadline=60.0, purpose=Purpose.TURN)


def nothing() -> ScriptedLLM:
    """A model that fails the test if it's ever called."""
    return ScriptedLLM([])


async def test_record_writes_one_file_per_call_and_replay_returns_them_unchanged(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    live = ScriptedLLM([Turn(tool_calls=(SIGNED,), usage=Usage(700, 30)), Turn(text="It shipped.")])
    recorder = wrap(live, store, Mode.RECORD)
    first = await ask(recorder)
    second = await ask(recorder, [*MESSAGES, {"role": "user", "content": "thanks"}])

    replayer = wrap(nothing(), store, Mode.REPLAY)
    assert await ask(replayer) == first
    assert await ask(replayer, [*MESSAGES, {"role": "user", "content": "thanks"}]) == second
    assert len(list(tmp_path.glob("*.json"))) == 2
    assert first.tool_calls == (SIGNED,)  # provider data survives the round trip


async def test_replay_with_no_recording_fails_with_its_key_and_calls_nothing(
    tmp_path: Path,
) -> None:
    inner = nothing()
    replayer = wrap(inner, RecordingStore(tmp_path), Mode.REPLAY)

    with pytest.raises(MissingRecording) as missing:
        await ask(replayer)

    assert missing.value.key == recording_key(**KEY_INPUTS)
    assert missing.value.key in str(missing.value)
    assert inner.calls == []


async def test_each_of_k_runs_replays_its_own_sample(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    for index in (1, 2, 3):
        await ask(
            wrap(ScriptedLLM([Turn(text=f"sample {index}")]), store, Mode.RECORD, run_index=index)
        )

    replayed = [
        (await ask(wrap(nothing(), store, Mode.REPLAY, run_index=index))).text
        for index in (1, 2, 3)
    ]

    assert replayed == ["sample 1", "sample 2", "sample 3"]


@pytest.mark.parametrize(
    "change",
    [
        {"fingerprints": {Purpose.TURN: "c" * 64, Purpose.SUMMARY: "b" * 64}},
        {"model": "another-model"},
    ],
    ids=["prompt fingerprint", "model"],
)
async def test_a_changed_prompt_or_model_misses_every_recording(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    store = RecordingStore(tmp_path)
    await ask(wrap(ScriptedLLM([Turn(text="old answer")]), store, Mode.RECORD))

    with pytest.raises(MissingRecording):
        await ask(wrap(nothing(), store, Mode.REPLAY, **change))


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "gemini"},
        {"messages": [*MESSAGES, {"role": "user", "content": "more"}]},
        {"tools": []},
        {"temperature": 0.3},
        {"max_output_tokens": 512},
        {"run_index": 2},
    ],
    ids=lambda change: next(iter(change)),
)
def test_every_key_input_changes_the_key(change: dict[str, Any]) -> None:
    assert recording_key(**{**KEY_INPUTS, **change}) != recording_key(**KEY_INPUTS)


def test_the_key_is_the_same_in_another_process() -> None:
    code = (
        "import json, sys\n"
        "from agent_testing.evals.recording import recording_key\n"
        "print(recording_key(**json.loads(sys.argv[1])))"
    )

    other = subprocess.run(
        [sys.executable, "-c", code, json.dumps(KEY_INPUTS)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "testing")},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert other.stdout.strip() == recording_key(**KEY_INPUTS)


async def test_a_recording_made_with_another_anchor_is_refused_naming_both(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    old_anchor = ANCHOR - timedelta(days=30)
    await ask(wrap(ScriptedLLM([Turn(text="old")]), store, Mode.RECORD, anchor=old_anchor))

    with pytest.raises(AnchorMismatch) as refused:
        await ask(wrap(nothing(), store, Mode.REPLAY))

    assert old_anchor.isoformat() in str(refused.value) and ANCHOR.isoformat() in str(refused.value)


@pytest.mark.parametrize("mode", list(Mode))
async def test_a_prompt_with_the_real_date_fails_before_any_call(
    tmp_path: Path, mode: Mode
) -> None:
    inner = nothing()
    leaky = [*MESSAGES, {"role": "system", "content": "Today is 2026-11-19."}]

    with pytest.raises(RealDateLeak, match="2026-11-19"):
        await ask(wrap(inner, RecordingStore(tmp_path), mode), leaky)

    assert inner.calls == [] and list(tmp_path.iterdir()) == []


async def test_the_anchors_date_is_not_a_leak(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    dated = [*MESSAGES, {"role": "system", "content": f"ticket ({ANCHOR.date().isoformat()})"}]

    await ask(wrap(ScriptedLLM([Turn(text="ok")]), store, Mode.RECORD), dated)


async def test_live_stores_nothing(tmp_path: Path) -> None:
    response = await ask(
        wrap(ScriptedLLM([Turn(text="fresh")]), RecordingStore(tmp_path), Mode.LIVE)
    )

    assert response.text == "fresh" and list(tmp_path.iterdir()) == []


async def test_a_call_that_fails_is_not_recorded(tmp_path: Path) -> None:
    recorder = wrap(
        ScriptedLLM([Turn(raises=RateLimited("slow down"))]), RecordingStore(tmp_path), Mode.RECORD
    )

    with pytest.raises(RateLimited):
        await ask(recorder)

    assert list(tmp_path.iterdir()) == []


def test_evals_are_refused_in_production() -> None:
    with pytest.raises(EvalRefused, match="APP_ENV=production"):
        refuse_production({"APP_ENV": "production"})
    refuse_production({"APP_ENV": "development"})

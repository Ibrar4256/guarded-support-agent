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
    Route,
    recording_key,
    refuse_production,
)
from agent_testing.scripted_llm import ScriptedLLM, Turn

from agent.llm.errors import RateLimited
from agent.llm.types import ModelResponse, Purpose, ToolCall, Usage

FINGERPRINTS = {Purpose.TURN: "a" * 64, Purpose.SUMMARY: "b" * 64, Purpose.WARRANT: "c" * 64}
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
    routes: dict[Purpose, Route] | None = None,
    record_purposes: frozenset[Purpose] = frozenset(),
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
        routes=routes or {},
        record_purposes=record_purposes,
    )


async def ask(
    model: EvalModel,
    messages: list[dict[str, str]] = MESSAGES,
    *,
    purpose: Purpose = Purpose.TURN,
) -> ModelResponse:
    return await model.complete(messages, TOOLS, deadline=60.0, purpose=purpose)


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


# --- record-missing and a second model (ADR-029 S4) ----------------------------------------------

WARRANT_ROUTE_MODEL = "gemini-2.5-flash"
MISSING = frozenset({Purpose.WARRANT, Purpose.SUMMARY})


def warrant_route(inner: ScriptedLLM) -> dict[Purpose, Route]:
    return {Purpose.WARRANT: Route("gemini", WARRANT_ROUTE_MODEL, inner)}


async def test_record_missing_replays_what_it_has_and_records_only_a_missing_named_purpose(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    recorded = await ask(wrap(ScriptedLLM([Turn(text="old turn")]), store, Mode.RECORD))
    turn_model, warrant_model = (
        nothing(),
        ScriptedLLM([Turn(text="a verdict", usage=Usage(90, 10))]),
    )
    filler = wrap(
        turn_model,
        store,
        Mode.RECORD_MISSING,
        routes=warrant_route(warrant_model),
        record_purposes=MISSING,
    )

    assert await ask(filler) == recorded, "the turn key it has is replayed, not called"
    verdict = await ask(filler, purpose=Purpose.WARRANT)

    assert verdict.text == "a verdict" and len(warrant_model.calls) == 1 and turn_model.calls == []
    assert len(list(tmp_path.glob("*.json"))) == 2, "the warrant call was recorded"
    assert filler.recorded == {"warrant": 1} and filler.replayed == {"turn": 1}
    again = wrap(nothing(), store, Mode.REPLAY, routes=warrant_route(nothing()))
    assert await ask(again, purpose=Purpose.WARRANT) == verdict, "and replays from now on"


async def test_record_missing_fails_on_a_missing_turn_key_and_calls_nothing(
    tmp_path: Path,
) -> None:
    """A changed turn prompt must not be recorded silently (ADR-029 S4)."""
    store = RecordingStore(tmp_path)
    await ask(wrap(ScriptedLLM([Turn(text="old turn")]), store, Mode.RECORD))
    turn_model = ScriptedLLM([Turn(text="a new answer to a changed prompt")])
    filler = wrap(turn_model, store, Mode.RECORD_MISSING, record_purposes=MISSING)

    with pytest.raises(MissingRecording):
        await ask(filler, [*MESSAGES, {"role": "user", "content": "a changed prompt"}])

    assert turn_model.calls == [] and len(list(tmp_path.glob("*.json"))) == 1


async def test_record_missing_fails_on_a_missing_key_of_a_purpose_it_wasnt_told_to_record(
    tmp_path: Path,
) -> None:
    summary_model = ScriptedLLM([Turn(text="a summary")])
    filler = wrap(
        summary_model,
        RecordingStore(tmp_path),
        Mode.RECORD_MISSING,
        record_purposes=frozenset({Purpose.WARRANT}),
    )

    with pytest.raises(MissingRecording):
        await ask(filler, purpose=Purpose.SUMMARY)

    assert summary_model.calls == []


async def test_record_missing_refuses_a_recording_made_with_another_anchor(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    old_anchor = ANCHOR - timedelta(days=30)
    await ask(wrap(ScriptedLLM([Turn(text="old")]), store, Mode.RECORD, anchor=old_anchor))

    with pytest.raises(AnchorMismatch):
        await ask(wrap(nothing(), store, Mode.RECORD_MISSING, record_purposes=MISSING))


async def test_a_purpose_routed_to_another_model_is_keyed_called_and_recorded_as_that_model(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    turn_model, warrant_model = nothing(), ScriptedLLM([Turn(text="a verdict")])
    recorder = wrap(turn_model, store, Mode.RECORD, routes=warrant_route(warrant_model))

    await ask(recorder, purpose=Purpose.WARRANT)

    expected = recording_key(
        **{
            **KEY_INPUTS,
            "provider": "gemini",
            "model": WARRANT_ROUTE_MODEL,
            "fingerprint": FINGERPRINTS[Purpose.WARRANT],
        }
    )
    assert recorder.keys == [expected]
    assert len(warrant_model.calls) == 1 and turn_model.calls == []
    saved = json.loads(store.path(expected).read_text())
    assert (saved["provider"], saved["model"], saved["purpose"]) == (
        "gemini",
        WARRANT_ROUTE_MODEL,
        "warrant",
    )


async def test_each_provider_is_charged_its_own_live_tokens_and_a_replayed_call_nothing(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    await ask(wrap(ScriptedLLM([Turn(text="old turn", usage=Usage(700, 30))]), store, Mode.RECORD))
    filler = wrap(
        nothing(),
        store,
        Mode.RECORD_MISSING,
        routes=warrant_route(ScriptedLLM([Turn(text="a verdict", usage=Usage(90, 10))])),
        record_purposes=MISSING,
    )

    await ask(filler)
    await ask(filler, purpose=Purpose.WARRANT)

    assert filler.spent == {"gemini": 100}, "the replayed turn's 730 tokens cost nothing"
    assert filler.uncounted == 0


async def test_a_live_call_without_full_usage_is_uncounted_and_a_replayed_one_is_not(
    tmp_path: Path,
) -> None:
    store = RecordingStore(tmp_path)
    await ask(wrap(ScriptedLLM([Turn(text="old turn")]), store, Mode.RECORD))  # no usage
    filler = wrap(
        nothing(),
        store,
        Mode.RECORD_MISSING,
        routes=warrant_route(ScriptedLLM([Turn(text="a verdict")])),
        record_purposes=MISSING,
    )

    await ask(filler)
    assert filler.uncounted == 0, "the provider counted nothing then, and nothing is spent now"
    await ask(filler, purpose=Purpose.WARRANT)

    assert filler.uncounted == 1 and filler.spent == {"gemini": 0}


async def test_every_recording_error_is_kept_even_when_the_caller_swallows_it(
    tmp_path: Path,
) -> None:
    """The background summary catches and logs its errors; the runner reads ``errors``."""
    replayer = wrap(nothing(), RecordingStore(tmp_path), Mode.REPLAY)

    with pytest.raises(MissingRecording) as missing:
        await ask(replayer, purpose=Purpose.SUMMARY)
    with pytest.raises(RealDateLeak):
        await ask(replayer, [*MESSAGES, {"role": "system", "content": "Today is 2026-11-19."}])

    assert [type(e) for e in replayer.errors] == [MissingRecording, RealDateLeak]
    assert replayer.errors[0] is missing.value

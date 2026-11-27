"""``replay --expect`` through the entry point (ADR-030 S2): a replay equal to its pinned report
exits 0, a difference exits 4 naming the case-run, and a recording error exits 1 with the
differences printed too. A small suite is recorded first with a scripted model, so nothing
leaves the machine; each case run still gets its own cloned database."""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from agent_testing.evals import __main__ as entry
from agent_testing.evals.budget import DAILY_VAR, RESERVE_VAR
from agent_testing.evals.databases import EvalDatabases
from agent_testing.evals.pin import DIFFERENT
from agent_testing.scripted_llm import ScriptedLLM, Turn, tool_call

from agent.llm.settings import LLMSettings, ProviderChoice
from agent.llm.types import ChatModel, Purpose, Usage

MODEL = "groq:openai/gpt-oss-120b"
ORDER = "ord_eval_1"
USAGE = Usage(input_tokens=400, output_tokens=50)
DELIVERED = {
    "id": ORDER,
    "charged_minor": 4000,
    "currency": "USD",
    "status": "delivered",
    "delivered": "-3d",
}


@pytest.fixture
def environ() -> Iterator[dict[str, str]]:
    if not os.environ.get("AGENT_DB_OWNER_URL"):
        pytest.skip("the agent database settings aren't set")
    if not os.environ.get("POSTGRES_SUPERUSER"):
        pytest.fail("evals clone databases as the superuser: POSTGRES_SUPERUSER isn't set")
    budget = {DAILY_VAR: "groq:100000", RESERVE_VAR: "groq:1"}
    # The model is a fake: record mode needs no real key; never depend on the developer's .env.
    yield {**os.environ, "GROQ_API_KEY": "fake-model-needs-no-key", **budget}
    left = EvalDatabases.from_env(os.environ).eval_databases()
    assert left == [], f"eval databases left behind (clones or templates): {left}"


def recorded_suite(root: Path, environ: dict[str, str]) -> Path:
    """Two status cases recorded at k=1; returns the report the recording run wrote."""
    folder = root / "cases" / "suite"
    folder.mkdir(parents=True)
    for case_id, message in (
        ("first", "Where is ord_eval_1?"),
        ("second", "Has ord_eval_1 arrived?"),
    ):
        body = {
            "category": "smoke",
            "setup": {"customer": "cust_eval", "orders": [DELIVERED]},
            "messages": [message],
            "expect": [{"looked_up": {"order": ORDER}}],
        }
        (folder / f"{case_id}.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    fake = ScriptedLLM(
        [
            Turn(
                tool_calls=(tool_call("lookup_order", {"order_id": ORDER}, "c1"),),
                usage=USAGE,
                match=lambda v: v.purpose is Purpose.TURN and not v.has_tool_results,
                repeat=True,
            ),
            Turn(
                text="Your order ord_eval_1 was delivered.",
                usage=USAGE,
                match=lambda v: v.purpose is Purpose.TURN and v.has_tool_results,
                repeat=True,
            ),
        ]
    )

    def inner(_: LLMSettings, __: ProviderChoice) -> ChatModel:
        return fake

    assert entry.main([*common(root), "--mode", "record"], environ, inner=inner) == 0
    (report,) = (root / "reports").glob("*.json")
    return report


def common(root: Path) -> list[str]:
    return [
        *("--suite", "suite", "--model", MODEL, "--k", "1"),
        *("--cases-root", str(root / "cases"), "--recordings", str(root / "recordings")),
        *("--progress", str(root / "progress"), "--reports", str(root / "reports")),
    ]


def replay(root: Path, environ: dict[str, str], expected: Path) -> int:
    keyless = {k: v for k, v in environ.items() if k != "GROQ_API_KEY"}
    return entry.main([*common(root), "--mode", "replay", "--expect", str(expected)], keyless)


def edit(path: Path, change: Any) -> None:
    summary = json.loads(path.read_text())
    change(summary)
    path.write_text(json.dumps(summary), encoding="utf-8")


def test_a_replay_equal_to_its_pinned_report_exits_0(
    tmp_path: Path, environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    report = recorded_suite(tmp_path, environ)

    assert replay(tmp_path, environ, report) == 0

    assert "no differences from the pinned report" in capsys.readouterr().out


def test_a_difference_exits_4_naming_the_case_run(
    tmp_path: Path, environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    report = recorded_suite(tmp_path, environ)
    edit(report, lambda s: s["runs"][1]["verdicts"][0].update(detail="looked up ['ord_x']"))
    capsys.readouterr()

    assert replay(tmp_path, environ, report) == DIFFERENT == 4

    out = capsys.readouterr().out
    assert (
        "second run 1: looked_up detail \"looked up ['ord_x']\" → \"looked up ['ord_eval_1']\""
        in out
    )


def test_a_recording_error_exits_1_and_still_prints_the_differences(
    tmp_path: Path, environ: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    report = recorded_suite(tmp_path, environ)
    recording = sorted((tmp_path / "recordings").glob("*.json"))[0]
    recording.unlink()
    capsys.readouterr()

    assert replay(tmp_path, environ, report) == 1

    out = capsys.readouterr().out
    assert "MissingRecording" in out
    assert "differences from the pinned report:" in out and " run 1: " in out

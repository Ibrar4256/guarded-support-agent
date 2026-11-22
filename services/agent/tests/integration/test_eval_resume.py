"""A recording run stops at its budget and resumes without repeating a finished case run (ADR-026
S6 and its test table). Drives ``python -m agent_testing.evals``'s ``main`` with a scripted fake
model, so nothing leaves the machine; each case run still gets its own cloned database."""

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from agent_testing.evals import __main__ as entry
from agent_testing.evals import budget as budget_module
from agent_testing.evals import runner as runner_module
from agent_testing.evals.budget import DAILY_VAR, RESERVE_VAR
from agent_testing.evals.databases import EvalDatabases
from agent_testing.scripted_llm import ScriptedLLM, Turn, tool_call

from agent.llm.errors import RateLimited
from agent.llm.settings import LLMSettings
from agent.llm.types import ChatModel, Purpose, Usage

MODEL = "groq:openai/gpt-oss-120b"
ORDER = "ord_eval_1"
DAY = "2026-11-22"
NEXT_DAY = "2026-11-23"
USAGE = Usage(input_tokens=400, output_tokens=50)  # a case run: two calls, 900 tokens
# A budget of 800: the first case run fits (0 < 800), the second waits (900 >= 800).
BUDGET = {DAILY_VAR: "groq:1300", RESERVE_VAR: "groq:500"}
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
    # The model is a fake: record mode needs no real key; never depend on the developer's .env.
    yield {**os.environ, "GROQ_API_KEY": "fake-model-needs-no-key", **BUDGET}
    left = EvalDatabases.from_env(os.environ).eval_databases()
    assert left == [], f"eval databases left behind (clones or templates): {left}"


def write_case(root: Path, case_id: str, message: str, **extra: Any) -> None:
    folder = root / "cases" / "suite"
    folder.mkdir(parents=True, exist_ok=True)
    body = {
        "category": "smoke",
        "setup": {"customer": "cust_eval", "orders": [DELIVERED]},
        "messages": [message],
        "expect": [{"looked_up": {"order": ORDER}}],
        **extra,
    }
    (folder / f"{case_id}.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


def counted_model(final_usage: Usage | None = USAGE) -> ScriptedLLM:
    """Looks the order up, then replies; every call reports ``USAGE`` unless told otherwise."""
    return ScriptedLLM(
        [
            Turn(
                text="A summary.",
                usage=USAGE,
                match=lambda v: v.purpose is Purpose.SUMMARY,
                repeat=True,
            ),
            Turn(
                tool_calls=(tool_call("lookup_order", {"order_id": ORDER}, "c1"),),
                usage=USAGE,
                match=lambda v: v.purpose is Purpose.TURN and not v.has_tool_results,
                repeat=True,
            ),
            Turn(
                text="Your order ord_eval_1 was delivered.",
                usage=final_usage,
                match=lambda v: v.purpose is Purpose.TURN and v.has_tool_results,
                repeat=True,
            ),
        ]
    )


def record(
    root: Path,
    environ: dict[str, str],
    fake: ScriptedLLM,
    day: str,
    monkeypatch: pytest.MonkeyPatch,
    *extra: str,
) -> int:
    def inner(_: LLMSettings) -> ChatModel:
        return fake

    monkeypatch.setattr(entry, "today", lambda: day)
    return entry.main(
        [
            "--suite",
            "suite",
            "--mode",
            "record",
            "--model",
            MODEL,
            "--cases-root",
            str(root / "cases"),
            "--recordings",
            str(root / "recordings"),
            "--progress",
            str(root / "progress"),
            "--reports",
            str(root / "reports"),
            *extra,
        ],
        environ,
        inner=inner,
    )


def progress_file(root: Path) -> dict[str, Any]:
    (path,) = (root / "progress").glob("*.json")
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def test_a_recording_run_stops_at_its_budget_and_resumes_the_next_day_without_repeats(
    tmp_path: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_case(tmp_path, "first", "Where is my order ord_eval_1?")
    write_case(tmp_path, "second", "Has ord_eval_1 arrived yet?")
    fake = counted_model()

    stopped = record(tmp_path, environ, fake, DAY, monkeypatch)

    assert stopped == entry.STOPPED_EARLY
    assert len(fake.calls) == 2, "one case run, then the budget was spent"
    first_day = progress_file(tmp_path)
    assert first_day["finished"] == [["first", 1]]
    assert first_day["tokens_used_today"] == {"groq": 900} and first_day["day"] == DAY
    assert not (tmp_path / "reports").exists(), "no report until every case run has finished"

    again = record(tmp_path, environ, fake, DAY, monkeypatch)

    assert again == entry.STOPPED_EARLY
    assert len(fake.calls) == 2, "the same day's budget is still spent: no call made"

    done = record(tmp_path, environ, fake, NEXT_DAY, monkeypatch)

    assert len(fake.calls) == 4, "only the unfinished case run was recorded the next day"
    assert done == 0
    assert not list((tmp_path / "progress").glob("*.json")), "a finished run leaves no progress"
    (report_file,) = (tmp_path / "reports").glob("*.json")
    report = json.loads(report_file.read_text())
    assert [(r["case"], r["run"], r["passed"]) for r in report["runs"]] == [
        ("first", 1, True),
        ("second", 1, True),
    ]
    assert report["usage"]["input_tokens"] == 1600 and report["usage"]["calls"] == 4
    recordings = [json.loads(p.read_text()) for p in (tmp_path / "recordings").glob("*.json")]
    assert len(recordings) == 4
    assert {r["recording_run"] for r in recordings} == {first_day["recording_run"]}
    assert report_file.stem.endswith(first_day["recording_run"])


def test_a_call_the_provider_didnt_count_stops_the_run_after_its_case(
    tmp_path: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_case(tmp_path, "first", "Where is my order ord_eval_1?")
    write_case(tmp_path, "second", "Has ord_eval_1 arrived yet?")
    fake = counted_model(final_usage=None)
    environ = {**environ, DAILY_VAR: "groq:100000", RESERVE_VAR: "groq:1"}

    stopped = record(tmp_path, environ, fake, DAY, monkeypatch)

    assert stopped == entry.STOPPED_EARLY
    assert len(fake.calls) == 2
    progress = progress_file(tmp_path)
    assert progress["finished"] == [["first", 1]]
    assert progress["results"][0]["uncounted_calls"] == 1
    assert progress["tokens_used_today"] == {"groq": 450}, "the counted call is still charged"


def test_a_scripted_response_is_not_an_uncounted_call(
    tmp_path: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    script = [{"tool_calls": [{"name": "lookup_orders", "arguments": {"order_id": ORDER}}]}]
    write_case(tmp_path, "scripted", "Where is my order ord_eval_1?", script=script)
    write_case(tmp_path, "plain", "Has ord_eval_1 arrived yet?")
    # In order (see the runner's scripted test): after the scripted response the model looks up.
    lookup = Turn(tool_calls=(tool_call("lookup_order", {"order_id": ORDER}, "c1"),), usage=USAGE)
    reply = Turn(text="Your order ord_eval_1 was delivered.", usage=USAGE)
    fake = ScriptedLLM([lookup, reply, replace(lookup), replace(reply)])
    environ = {**environ, DAILY_VAR: "groq:100000", RESERVE_VAR: "groq:1"}

    done = record(tmp_path, environ, fake, DAY, monkeypatch)

    assert done == 0, "the scripted response carries no usage but costs nothing"
    assert len(fake.calls) == 4


def test_a_failed_provider_call_leaves_its_case_run_pending_and_the_resume_retries_it(
    tmp_path: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_case(tmp_path, "first", "Where is my order ord_eval_1?")
    lookup = Turn(tool_calls=(tool_call("lookup_order", {"order_id": ORDER}, "c1"),), usage=USAGE)
    limited = Turn(raises=RateLimited("429: tokens per minute"))
    reply = Turn(text="Your order ord_eval_1 was delivered.", usage=USAGE)
    # The adapter's retries are inside the real model; the fake's one raise is the call failing.
    fake = ScriptedLLM([lookup, limited, replace(lookup), reply])
    environ = {**environ, DAILY_VAR: "groq:100000", RESERVE_VAR: "groq:1"}

    stopped = record(tmp_path, environ, fake, DAY, monkeypatch)

    assert stopped == entry.STOPPED_EARLY
    progress = progress_file(tmp_path)
    assert progress["finished"] == [], "a rate limit isn't the model's answer"
    assert progress["tokens_used_today"] == {"groq": 450}, "the call that worked is charged"

    done = record(tmp_path, environ, fake, DAY, monkeypatch)

    assert len(fake.calls) == 4, "the resume ran the pending case run again"
    assert done == 0
    (report_file,) = (tmp_path / "reports").glob("*.json")
    report = json.loads(report_file.read_text())
    assert [(r["case"], r["passed"]) for r in report["runs"]] == [("first", True)]


def test_the_pause_comes_before_every_turn_but_the_runs_first(
    tmp_path: Path, environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "cases" / "suite"
    write_case(tmp_path, "first", "Where is my order ord_eval_1?")
    body = yaml.safe_load((folder / "first.yaml").read_text())
    body["messages"].append("And when did it arrive?")
    (folder / "first.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    write_case(tmp_path, "second", "Has ord_eval_1 arrived yet?")
    pauses: list[float] = []
    calls_at_pause: list[int] = []
    fake = counted_model()

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)
        calls_at_pause.append(len(fake.calls))

    monkeypatch.setattr(budget_module, "sleep", sleep)
    monkeypatch.setattr(runner_module, "sleep", sleep)
    environ = {**environ, DAILY_VAR: "groq:100000", RESERVE_VAR: "groq:1"}

    done = record(tmp_path, environ, fake, DAY, monkeypatch, "--pause", "7")

    assert done == 0
    assert pauses == [7.0, 7.0], "one pause before each of the three turns but the first"
    assert calls_at_pause == [2, 4], "each after a turn's two calls, before the next turn's"

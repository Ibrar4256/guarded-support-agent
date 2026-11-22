"""A recording run's budget and progress file (ADR-026 S6 and its test table, ADR-025 S7)."""

import json
from pathlib import Path

import pytest
import yaml
from agent_testing.evals.__main__ import main
from agent_testing.evals.budget import (
    DAILY_VAR,
    RESERVE_VAR,
    BudgetRefused,
    budget_from_env,
    exclusive,
    load_or_start,
    progress_key,
    run_identity,
    token_map,
)
from agent_testing.evals.judges import Verdict
from agent_testing.evals.runner import CaseRunResult

MODEL = "groq:openai/gpt-oss-120b"
DAY = "2026-11-22"
NEXT_DAY = "2026-11-23"
FINGERPRINTS = {"turn": "f" * 64, "summary": "e" * 64}


def identity(**case_files: bytes) -> dict[str, object]:
    return run_identity(
        suite="suite",
        model=MODEL,
        k=2,
        case_files=case_files or {"a": b"a: 1\n", "b": b"b: 1\n"},
        fingerprints=FINGERPRINTS,
    )


def result(case_id: str, run_index: int, tokens: int = 100) -> CaseRunResult:
    return CaseRunResult(
        case_id,
        "smoke",
        run_index,
        verdicts=[Verdict("looked_up", True, "ord_1")],
        keys=["k1"],
        calls=1,
        input_tokens=tokens - 10,
        output_tokens=10,
        latencies_ms=[5],
    )


# --- settings ------------------------------------------------------------------------------------


def test_the_token_lists_parse_known_providers_once_each() -> None:
    assert token_map({DAILY_VAR: " groq:200000, gemini:5 "}, DAILY_VAR) == {
        "groq": 200000,
        "gemini": 5,
    }
    assert token_map({}, DAILY_VAR) == {}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("groq", "is not known_provider:tokens"),
        ("acme:5", "is not known_provider:tokens"),
        ("groq:5,groq:6", "listed twice"),
        ("groq:-5", "whole number"),
        ("groq:1e5", "whole number"),
    ],
)
def test_a_malformed_token_list_is_refused(raw: str, message: str) -> None:
    with pytest.raises(BudgetRefused, match=message):
        token_map({DAILY_VAR: raw}, DAILY_VAR)


def test_the_budget_is_the_daily_limit_minus_the_reserve() -> None:
    budget = budget_from_env({DAILY_VAR: "groq:200000", RESERVE_VAR: "groq:50000"}, "groq")
    assert (budget.daily, budget.reserve, budget.tokens) == (200000, 50000, 150000)


@pytest.mark.parametrize("reserve", ["200000", "250000"])
def test_a_budget_that_would_cut_into_the_demo_reserve_is_refused(reserve: str) -> None:
    environ = {DAILY_VAR: "groq:200000", RESERVE_VAR: f"groq:{reserve}"}
    with pytest.raises(BudgetRefused, match="would cut into the demo reserve"):
        budget_from_env(environ, "groq")


@pytest.mark.parametrize(
    "environ",
    [
        {RESERVE_VAR: "groq:1"},
        {DAILY_VAR: "groq:10"},
        {DAILY_VAR: "gemini:10", RESERVE_VAR: "gemini:1"},
    ],
)
def test_a_provider_missing_from_either_list_is_refused(environ: dict[str, str]) -> None:
    with pytest.raises(BudgetRefused, match="needs groq in"):
        budget_from_env(environ, "groq")


# --- the progress file ---------------------------------------------------------------------------


def test_a_case_run_result_round_trips_through_json() -> None:
    original = result("a", 2)
    original.error = "TimeoutError: slow"
    original.uncounted_calls = 1
    loaded = CaseRunResult.from_json(json.loads(json.dumps(original.to_json())))
    assert loaded == original and loaded.verdicts[0] == Verdict("looked_up", True, "ord_1")


def test_a_resumed_run_keeps_its_id_finished_runs_and_the_days_tokens(tmp_path: Path) -> None:
    started, resumed = load_or_start(tmp_path, identity(), "groq", DAY)
    assert not resumed and started.used() == 0
    started.resumed_at("abc1234")
    started.record(result("a", 1, tokens=300))
    started.save()

    again, resumed = load_or_start(tmp_path, identity(), "groq", DAY)

    assert resumed
    assert again.recording_run == started.recording_run
    assert again.results == started.results and again.used() == 300
    assert again.pending(["a", "b"], 2) == [("a", 2), ("b", 1), ("b", 2)]
    assert again.commits == ["abc1234"]
    assert not list(tmp_path.glob("*.tmp")), "the temporary file is renamed over the progress"


def test_a_charged_but_unfinished_case_run_stays_pending(tmp_path: Path) -> None:
    started, _ = load_or_start(tmp_path, identity(), "groq", DAY)
    started.charge(result("a", 1, tokens=300))
    started.save()

    again, _ = load_or_start(tmp_path, identity(), "groq", DAY)

    assert again.used() == 300, "what a failed attempt spent stays charged"
    assert again.pending(["a"], 1) == [("a", 1)] and again.results == []


def test_the_days_counter_resets_on_a_new_day_and_finished_runs_stay(tmp_path: Path) -> None:
    started, _ = load_or_start(tmp_path, identity(), "groq", DAY)
    started.record(result("a", 1, tokens=300))
    started.save()

    next_day, _ = load_or_start(tmp_path, identity(), "groq", NEXT_DAY)

    assert (next_day.day, next_day.used()) == (NEXT_DAY, 0)
    assert next_day.finished == {("a", 1)}


def test_a_changed_case_file_or_prompt_starts_a_new_run(tmp_path: Path) -> None:
    base = progress_key(identity())
    assert progress_key(identity(a=b"a: 2\n", b=b"b: 1\n")) != base
    changed = run_identity(
        suite="suite",
        model=MODEL,
        k=2,
        case_files={"a": b"a: 1\n", "b": b"b: 1\n"},
        fingerprints={**FINGERPRINTS, "turn": "0" * 64},
    )
    assert progress_key(changed) != base


def test_a_progress_file_for_another_run_under_this_key_is_refused(tmp_path: Path) -> None:
    started, _ = load_or_start(tmp_path, identity(), "groq", DAY)
    started.save()
    data = json.loads(started.path.read_text())
    data["identity"]["k"] = 3
    started.path.write_text(json.dumps(data))

    with pytest.raises(BudgetRefused, match="belongs to another run"):
        load_or_start(tmp_path, identity(), "groq", DAY)


def test_a_progress_file_whose_finished_list_disagrees_is_refused(tmp_path: Path) -> None:
    started, _ = load_or_start(tmp_path, identity(), "groq", DAY)
    started.record(result("a", 1))
    started.save()
    data = json.loads(started.path.read_text())
    data["finished"].append(["b", 1])
    started.path.write_text(json.dumps(data))

    with pytest.raises(BudgetRefused, match="disagree"):
        load_or_start(tmp_path, identity(), "groq", DAY)


def test_a_second_runner_on_the_same_progress_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "key.json"
    with exclusive(path), pytest.raises(BudgetRefused, match="another runner"), exclusive(path):
        pass
    with exclusive(path):  # released
        pass


# --- the entry point refuses before any work ------------------------------------------------------


def test_a_recording_run_that_would_cut_into_the_reserve_is_refused_at_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "cases" / "suite"
    folder.mkdir(parents=True)
    case = {
        "category": "smoke",
        "setup": {"customer": "cust_eval"},
        "messages": ["Hello"],
        "expect": [{"no_refund_approved": {}}],
    }
    (folder / "hello.yaml").write_text(yaml.safe_dump(case), encoding="utf-8")
    # No database settings at all: the refusal must come before any database work.
    environ = {DAILY_VAR: "groq:1000", RESERVE_VAR: "groq:1000", "GROQ_API_KEY": "unused"}

    code = main(
        [
            "--suite",
            "suite",
            "--mode",
            "record",
            "--model",
            MODEL,
            "--cases-root",
            str(tmp_path / "cases"),
            "--progress",
            str(tmp_path / "progress"),
            "--reports",
            str(tmp_path / "reports"),
        ],
        environ,
    )

    assert code == 2
    assert "would cut into the demo reserve" in capsys.readouterr().err
    assert not (tmp_path / "progress").exists() and not (tmp_path / "reports").exists()

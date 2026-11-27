"""A recording run's budget and progress file (ADR-026 S6 and its test table, ADR-025 S7)."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from agent_testing.evals.__main__ import main
from agent_testing.evals.budget import (
    DAILY_VAR,
    RESERVE_VAR,
    Budget,
    BudgetRefused,
    budget_from_env,
    budgets_from_env,
    exclusive,
    live_providers,
    load_or_start,
    progress_key,
    run_identity,
    token_map,
)
from agent_testing.evals.cases import CaseError
from agent_testing.evals.judges import Verdict
from agent_testing.evals.recording import Mode
from agent_testing.evals.runner import CaseRunResult, RunConfig

from agent.llm.types import Purpose

MODEL = "groq:openai/gpt-oss-120b"
DAY = "2026-11-22"
NEXT_DAY = "2026-11-23"
WARRANT_MODEL = "gemini:gemini-2.5-flash"
FINGERPRINTS = {"turn": "f" * 64, "summary": "e" * 64, "warrant": "d" * 64}
GROQ = ("groq",)
IDENTITY_DEFAULTS: dict[str, Any] = {
    "warrant_model": MODEL,
    "mode": "record",
    "record_purposes": [],
}


def identity(**case_files: bytes) -> dict[str, object]:
    return run_identity(
        suite="suite",
        model=MODEL,
        k=2,
        case_files=case_files or {"a": b"a: 1\n", "b": b"b: 1\n"},
        fingerprints=FINGERPRINTS,
        **IDENTITY_DEFAULTS,
    )


def result(
    case_id: str, run_index: int, tokens: int = 100, spent: dict[str, int] | None = None
) -> CaseRunResult:
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
        spent={"groq": tokens} if spent is None else spent,
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
    original.spent = {"groq": 90, "gemini": 10}
    original.approved = True
    original.false_complaint = True
    original.proposals = [
        {
            "outcome": "REVIEW",
            "rule_ids": ["W01_NOT_WARRANTED"],
            "warrant": "other_order",
            "c02": [],
        }
    ]
    loaded = CaseRunResult.from_json(json.loads(json.dumps(original.to_json())))
    assert loaded == original and loaded.verdicts[0] == Verdict("looked_up", True, "ord_1")


def test_a_resumed_run_keeps_its_id_finished_runs_and_the_days_tokens(tmp_path: Path) -> None:
    started, resumed = load_or_start(tmp_path, identity(), GROQ, DAY)
    assert not resumed and started.used("groq") == 0
    started.resumed_at("abc1234")
    started.record(result("a", 1, tokens=300))
    started.save()

    again, resumed = load_or_start(tmp_path, identity(), GROQ, DAY)

    assert resumed
    assert again.recording_run == started.recording_run
    assert again.results == started.results and again.used("groq") == 300
    assert again.pending(["a", "b"], 2) == [("a", 2), ("b", 1), ("b", 2)]
    assert again.commits == ["abc1234"]
    assert not list(tmp_path.glob("*.tmp")), "the temporary file is renamed over the progress"


def test_a_charged_but_unfinished_case_run_stays_pending(tmp_path: Path) -> None:
    started, _ = load_or_start(tmp_path, identity(), GROQ, DAY)
    started.charge(result("a", 1, tokens=300))
    started.save()

    again, _ = load_or_start(tmp_path, identity(), GROQ, DAY)

    assert again.used("groq") == 300, "what a failed attempt spent stays charged"
    assert again.pending(["a"], 1) == [("a", 1)] and again.results == []


def test_the_days_counter_resets_on_a_new_day_and_finished_runs_stay(tmp_path: Path) -> None:
    started, _ = load_or_start(tmp_path, identity(), GROQ, DAY)
    started.record(result("a", 1, tokens=300))
    started.save()

    next_day, _ = load_or_start(tmp_path, identity(), GROQ, NEXT_DAY)

    assert (next_day.day, next_day.used("groq")) == (NEXT_DAY, 0)
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
        **IDENTITY_DEFAULTS,
    )
    assert progress_key(changed) != base


def test_a_progress_file_for_another_run_under_this_key_is_refused(tmp_path: Path) -> None:
    started, _ = load_or_start(tmp_path, identity(), GROQ, DAY)
    started.save()
    data = json.loads(started.path.read_text())
    data["identity"]["k"] = 3
    started.path.write_text(json.dumps(data))

    with pytest.raises(BudgetRefused, match="belongs to another run"):
        load_or_start(tmp_path, identity(), GROQ, DAY)


def test_a_progress_file_whose_finished_list_disagrees_is_refused(tmp_path: Path) -> None:
    started, _ = load_or_start(tmp_path, identity(), GROQ, DAY)
    started.record(result("a", 1))
    started.save()
    data = json.loads(started.path.read_text())
    data["finished"].append(["b", 1])
    started.path.write_text(json.dumps(data))

    with pytest.raises(BudgetRefused, match="disagree"):
        load_or_start(tmp_path, identity(), GROQ, DAY)


def test_each_provider_is_charged_what_the_case_run_spent_with_it(tmp_path: Path) -> None:
    """ADR-029 S4: the live tally, not ``--model``'s provider, decides who pays."""
    started, _ = load_or_start(tmp_path, identity(), ("gemini", "groq"), DAY)
    started.record(result("a", 1, spent={"groq": 0, "gemini": 110}))
    started.charge(result("b", 1, spent={"groq": 400, "gemini": 50}))
    started.save()

    again, _ = load_or_start(tmp_path, identity(), ("gemini", "groq"), DAY)

    assert again.tokens_used_today == {"groq": 400, "gemini": 160}
    assert (again.used("groq"), again.used("gemini"), again.used("cloudflare")) == (400, 160, 0)


def test_the_run_stops_once_any_budgeted_provider_has_spent_its_budget(tmp_path: Path) -> None:
    budgets = {"groq": Budget("groq", 1000, 0), "gemini": Budget("gemini", 100, 0)}
    started, _ = load_or_start(tmp_path, identity(), ("gemini", "groq"), DAY)
    started.charge(result("a", 1, spent={"groq": 10, "gemini": 99}))
    assert started.exhausted(budgets) is None

    started.charge(result("a", 1, spent={"gemini": 1}))

    stopped = started.exhausted(budgets)
    assert stopped is not None and "gemini" in stopped and "groq" not in stopped


def test_a_progress_file_budgeted_for_other_providers_is_refused(tmp_path: Path) -> None:
    started, _ = load_or_start(tmp_path, identity(), ("gemini", "groq"), DAY)
    started.save()

    with pytest.raises(BudgetRefused, match="belongs to another run"):
        load_or_start(tmp_path, identity(), GROQ, DAY)


def test_an_old_version_progress_file_is_refused_naming_both_versions(tmp_path: Path) -> None:
    started, _ = load_or_start(tmp_path, identity(), GROQ, DAY)
    started.save()
    data = json.loads(started.path.read_text())
    data["version"] = 1
    started.path.write_text(json.dumps(data))

    with pytest.raises(BudgetRefused, match="version 1 isn't 2"):
        load_or_start(tmp_path, identity(), GROQ, DAY)


@pytest.mark.parametrize(
    "change",
    [
        {"warrant_model": WARRANT_MODEL},
        {"mode": "record-missing"},
        {"record_purposes": ["summary", "warrant"]},
    ],
    ids=lambda change: next(iter(change)),
)
def test_the_warrant_model_mode_and_recorded_purposes_start_a_new_run(
    change: dict[str, Any],
) -> None:
    def key(**values: Any) -> str:
        return progress_key(
            run_identity(
                suite="suite",
                model=MODEL,
                k=2,
                case_files={"a": b"a: 1\n"},
                fingerprints=FINGERPRINTS,
                **{**IDENTITY_DEFAULTS, **values},
            )
        )

    assert key(**change) != key()


@pytest.mark.parametrize(
    ("mode", "warrant_model", "purposes", "expected"),
    [
        (Mode.RECORD, None, frozenset(), ("groq",)),
        (Mode.RECORD, WARRANT_MODEL, frozenset(), ("gemini", "groq")),
        (Mode.RECORD_MISSING, WARRANT_MODEL, frozenset({Purpose.WARRANT}), ("gemini",)),
        (
            Mode.RECORD_MISSING,
            WARRANT_MODEL,
            frozenset({Purpose.WARRANT, Purpose.SUMMARY}),
            ("gemini", "groq"),
        ),
        (Mode.RECORD_MISSING, None, frozenset({Purpose.WARRANT}), ("groq",)),
    ],
)
def test_a_run_budgets_every_provider_it_may_call_live_and_no_other(
    mode: Mode, warrant_model: str | None, purposes: frozenset[Purpose], expected: tuple[str, ...]
) -> None:
    config = RunConfig("suite", mode, MODEL, warrant_model=warrant_model, record_purposes=purposes)
    assert live_providers(config) == expected


def test_budgets_refuse_when_any_live_provider_has_no_entry() -> None:
    environ = {DAILY_VAR: "groq:1000", RESERVE_VAR: "groq:10"}
    with pytest.raises(BudgetRefused, match="needs gemini in"):
        budgets_from_env(environ, ("gemini", "groq"))
    assert budgets_from_env(environ, GROQ) == {"groq": Budget("groq", 1000, 10)}


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


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--mode", "replay", "--pause", "5"], "record and record-missing only"),
        (["--mode", "record", "--pause", "-1"], "between 0 and"),
        (["--mode", "record", "--pause", "601"], "between 0 and"),
    ],
)
def test_a_pause_outside_record_mode_or_its_bounds_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    with pytest.raises(SystemExit):
        main(["--suite", "suite", "--model", MODEL, *argv], {})
    assert message in capsys.readouterr().err


def case_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "cases" / "suite"
    folder.mkdir(parents=True)
    case = {
        "category": "smoke",
        "setup": {"customer": "cust_eval"},
        "messages": ["Hello"],
        "expect": [{"no_refund_approved": {}}],
    }
    (folder / "hello.yaml").write_text(yaml.safe_dump(case), encoding="utf-8")
    return tmp_path / "cases"


@pytest.mark.parametrize(
    ("mode", "purposes"),
    [("record-missing", "warrant"), ("record", None)],
)
def test_a_run_whose_warrant_provider_has_no_budget_is_refused_at_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str, purposes: str | None
) -> None:
    # No database settings at all: the refusal must come before any database work.
    environ = {
        DAILY_VAR: "groq:1000",
        RESERVE_VAR: "groq:10",
        "GROQ_API_KEY": "unused",
        "GEMINI_API_KEY": "unused",
    }
    extra = ["--record-purposes", purposes] if purposes else []

    code = main(
        [
            "--suite",
            "suite",
            "--mode",
            mode,
            "--model",
            MODEL,
            "--warrant-model",
            WARRANT_MODEL,
            *extra,
            "--cases-root",
            str(case_folder(tmp_path)),
            "--progress",
            str(tmp_path / "progress"),
            "--reports",
            str(tmp_path / "reports"),
        ],
        environ,
    )

    assert code == 2
    assert "needs gemini in" in capsys.readouterr().err
    assert not (tmp_path / "progress").exists() and not (tmp_path / "reports").exists()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--mode", "replay", "--record-purposes", "warrant"], "record-missing mode only"),
        (["--mode", "record", "--record-purposes", "warrant"], "record-missing mode only"),
        (["--mode", "record-missing", "--record-purposes", "turn"], "turn calls are never"),
        (["--mode", "record-missing", "--record-purposes", "warrant,turn"], "turn calls are never"),
        (["--mode", "record-missing", "--record-purposes", "warant"], "unknown purpose"),
        (["--mode", "record-missing", "--record-purposes", ""], "unknown purpose"),
    ],
)
def test_record_purposes_outside_record_missing_or_naming_turn_are_refused(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    with pytest.raises(SystemExit):
        main(["--suite", "suite", "--model", MODEL, *argv], {})
    assert message in capsys.readouterr().err


def test_the_pause_is_allowed_in_record_missing_mode(tmp_path: Path) -> None:
    """It calls live too: the arguments pass, and only the missing suite stops it."""
    with pytest.raises(CaseError, match="no cases in"):
        main(
            [
                *("--suite", "suite", "--model", MODEL, "--mode", "record-missing"),
                *("--pause", "5", "--cases-root", str(tmp_path)),
            ],
            {},
        )

"""The eval replay gate's pin and comparison (ADR-030 S1, S2, S3): which report each suite is
pinned to, what makes a pin valid, and what counts as a difference between a pinned report and
a replay (or between an old and a new pin)."""

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from agent_testing.evals.judges import Verdict
from agent_testing.evals.pin import PinError, compare, load_pins, main, moved
from agent_testing.evals.recording import Mode
from agent_testing.evals.report import summarize
from agent_testing.evals.runner import EVALS, CaseRunResult, RunConfig

MODEL = "groq:openai/gpt-oss-120b"
WARRANT_MODEL = "gemini:gemini-2.5-flash"


def result(case_id: str, index: int, *, approved: bool = False, ok: bool = True) -> CaseRunResult:
    run = CaseRunResult(case_id, "direct_injection", index)
    run.verdicts = [
        Verdict("looked_up", True, "looked up ['ord_1']"),
        Verdict("no_refund_approved", ok, "" if ok else "approved 2000 on ord_1"),
    ]
    run.approved = approved
    return run


def report(
    suite: str = "adversarial", mode: Mode = Mode.RECORD_MISSING, k: int = 2
) -> dict[str, Any]:
    results = [
        result("a", 1),
        result("a", 2),
        result("b", 1, approved=True, ok=False),
        result("b", 2),
    ]
    config = RunConfig(suite, mode, MODEL, k=k, warrant_model=WARRANT_MODEL)
    return summarize(results, config=config, commit="abc1234", made_on="2026-11-27")


def run_of(summary: dict[str, Any], case: str, index: int) -> dict[str, Any]:
    found: list[dict[str, Any]] = [
        r for r in summary["runs"] if (r["case"], r["run"]) == (case, index)
    ]
    assert len(found) == 1, found
    return found[0]


# --- the comparison (S2) --------------------------------------------------------------------------


def test_a_replay_equal_to_its_pin_has_no_differences() -> None:
    assert compare(report(), report()) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param(
            lambda s: run_of(s, "a", 2)["verdicts"][0].update(detail="looked up ['ord_2']"),
            "a run 2: looked_up detail \"looked up ['ord_1']\" → \"looked up ['ord_2']\"",
            id="detail",
        ),
        pytest.param(
            lambda s: run_of(s, "a", 1)["verdicts"][1].update(passed=False),
            "a run 1: no_refund_approved passed True → False",
            id="verdict-pass",
        ),
        pytest.param(
            lambda s: run_of(s, "b", 1).update(bucket="stopped_by_warrant"),
            "b run 1: bucket approved_without_complaint → stopped_by_warrant",
            id="bucket",
        ),
        pytest.param(
            lambda s: run_of(s, "b", 2).update(passed=False),
            "b run 2: passed True → False",
            id="run-pass",
        ),
        pytest.param(
            lambda s: run_of(s, "a", 1)["verdicts"].pop(),
            "a run 1: judges ['looked_up', 'no_refund_approved'] → ['looked_up']",
            id="verdict-gone",
        ),
    ],
)
def test_each_difference_in_a_case_run_is_named(change: Any, expected: str) -> None:
    replayed = report()
    change(replayed)

    differences = compare(report(), replayed)

    assert expected in differences, differences


def test_a_case_run_on_one_side_only_is_named_either_way() -> None:
    pinned, replayed = report(), report()
    replayed["runs"] = [r for r in replayed["runs"] if (r["case"], r["run"]) != ("b", 2)]
    pinned["runs"] = [r for r in pinned["runs"] if (r["case"], r["run"]) != ("a", 1)]

    differences = compare(pinned, replayed)

    assert "b run 2: in the pinned report only" in differences
    assert "a run 1: in the replay only" in differences


@pytest.mark.parametrize("total", ["categories", "judges", "split", "reviews_by_cause"])
def test_a_total_that_differs_while_every_case_run_matches_is_named(total: str) -> None:
    """A summing bug leaves every case-run equal and the headline numbers wrong (ADR-030)."""
    replayed = report()
    replayed[total] = {**replayed[total], "planted": 1}

    differences = compare(report(), replayed)

    assert differences and all(d.startswith(f"{total}:") for d in differences), differences


def test_run_metadata_never_counts_as_a_difference() -> None:
    replayed = copy.deepcopy(report())
    replayed.update(
        mode="replay",
        commit="def5678+dirty",
        date="2026-12-01",
        record_purposes=["summary", "warrant"],
        keys={"recorded": {}, "replayed": {"turn": 9}},
    )
    replayed["usage"] = {**replayed["usage"], "input_tokens": 999, "latency_ms_p50": 1}

    assert compare(report(), replayed) == []


def test_another_suite_is_a_difference() -> None:
    assert compare(report(), report(suite="resolution")) == ["suite: adversarial → resolution"]


# --- the pin and its validation (S1) --------------------------------------------------------------


def evals_tree(tmp_path: Path, pins: dict[str, str], suites: tuple[str, ...]) -> Path:
    for suite in suites:
        folder = tmp_path / "cases" / suite
        folder.mkdir(parents=True)
        (folder / "one.yaml").write_text("category: smoke\n", encoding="utf-8")
    (tmp_path / "reports").mkdir()
    (tmp_path / "pinned.json").write_text(json.dumps(pins), encoding="utf-8")
    return tmp_path


def put_report(root: Path, name: str, summary: dict[str, Any]) -> None:
    (root / "reports" / name).write_text(json.dumps(summary), encoding="utf-8")


def test_a_valid_pin_gives_each_suite_its_report_k_and_models(tmp_path: Path) -> None:
    root = evals_tree(
        tmp_path, {"adversarial": "adv.json", "smoke": "smoke.json"}, ("adversarial", "smoke")
    )
    put_report(root, "adv.json", report())
    put_report(root, "smoke.json", report(suite="smoke", k=1))

    pins = load_pins(root)

    assert [(p.suite, p.report.name, p.k, p.model, p.warrant_model) for p in pins] == [
        ("adversarial", "adv.json", 2, MODEL, WARRANT_MODEL),
        ("smoke", "smoke.json", 1, MODEL, WARRANT_MODEL),
    ]


def drop_buckets(summary: dict[str, Any]) -> dict[str, Any]:
    for run in summary["runs"]:
        del run["bucket"]
    return summary


def with_errors(summary: dict[str, Any]) -> dict[str, Any]:
    summary["errors"] = [{"case": "a", "run": 1, "error": "MissingRecording: no recording"}]
    return summary


@pytest.mark.parametrize(
    ("pins", "suites", "written", "message"),
    [
        pytest.param(
            {}, ("adversarial",), {}, "adversarial has cases but no pin", id="suite-not-pinned"
        ),
        pytest.param(
            {"adversarial": "adv.json", "gone": "adv.json"},
            ("adversarial",),
            {"adv.json": report()},
            "gone is pinned but has no cases",
            id="pin-without-suite",
        ),
        pytest.param(
            {"adversarial": "nope.json"},
            ("adversarial",),
            {},
            "no such report",
            id="missing-report",
        ),
        pytest.param(
            {"adversarial": "res.json"},
            ("adversarial",),
            {"res.json": report(suite="resolution")},
            "names suite resolution",
            id="another-suite",
        ),
        pytest.param(
            {"adversarial": "live.json"},
            ("adversarial",),
            {"live.json": report(mode=Mode.LIVE)},
            "not written by a recording run",
            id="not-a-recording-run",
        ),
        pytest.param(
            {"adversarial": "err.json"},
            ("adversarial",),
            {"err.json": with_errors(report())},
            "has errors",
            id="errors",
        ),
        pytest.param(
            {"adversarial": "old.json"},
            ("adversarial",),
            {"old.json": drop_buckets(report())},
            "has no split buckets",
            id="before-s4",
        ),
        pytest.param(
            {"adversarial": "../outside.json"},
            ("adversarial",),
            {},
            "a file name in reports/",
            id="path-escape",
        ),
    ],
)
def test_a_wrong_pin_is_refused_naming_why(
    tmp_path: Path,
    pins: dict[str, str],
    suites: tuple[str, ...],
    written: dict[str, dict[str, Any]],
    message: str,
) -> None:
    root = evals_tree(tmp_path, pins, suites)
    for name, summary in written.items():
        put_report(root, name, summary)

    with pytest.raises(PinError, match=re.escape(message)):
        load_pins(root)


@pytest.mark.parametrize("content", ["[]", '{"adversarial": 3}', "not json"])
def test_a_malformed_pin_file_is_refused(tmp_path: Path, content: str) -> None:
    root = evals_tree(tmp_path, {}, ("adversarial",))
    (root / "pinned.json").write_text(content, encoding="utf-8")

    with pytest.raises(PinError, match=re.escape("pinned.json")):
        load_pins(root)


def test_the_committed_pin_is_valid() -> None:
    """A broken pin fails the agent's own checks too, not only the eval job."""
    pins = {p.suite: p for p in load_pins(EVALS)}

    assert sorted(pins) == ["adversarial", "resolution", "smoke"]
    assert pins["smoke"].report.name.endswith("_598f4e0e0fd9.json") and pins["smoke"].k == 1
    assert pins["adversarial"].report.name.endswith("_2e43a1c004d4.json")
    assert pins["resolution"].report.name.endswith("_8f42152483fa.json")
    assert {p.k for p in (pins["adversarial"], pins["resolution"])} == {3}


def test_list_prints_what_the_ci_script_needs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = evals_tree(tmp_path, {"adversarial": "adv.json"}, ("adversarial",))
    put_report(root, "adv.json", report())

    assert main(["list", "--evals", str(root)]) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"adversarial 2 {MODEL} {WARRANT_MODEL} {root / 'reports' / 'adv.json'}"
    ]


def test_list_on_a_wrong_pin_exits_non_zero_naming_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = evals_tree(tmp_path, {}, ("adversarial",))

    assert main(["list", "--evals", str(root)]) == 2

    assert "adversarial has cases but no pin" in capsys.readouterr().err


# --- what a re-pin moved (S3) ---------------------------------------------------------------------


def test_an_unchanged_pin_moved_nothing() -> None:
    pins = {"adversarial": "adv.json"}
    assert moved(pins, pins, read_old=lambda _: report(), read_new=lambda _: report()) == []


def test_a_re_pin_lists_the_case_runs_it_moved_by_suite() -> None:
    new = report()
    run_of(new, "b", 1).update(bucket="stopped_by_warrant")

    lines = moved(
        {"adversarial": "old.json", "smoke": "s.json"},
        {"adversarial": "new.json", "smoke": "s.json"},
        read_old=lambda name: report(),
        read_new=lambda name: new if name == "new.json" else report(),
    )

    assert "adversarial: b run 1: bucket approved_without_complaint → stopped_by_warrant" in lines
    assert not [line for line in lines if line.startswith("smoke")], "an unchanged pin isn't read"


def test_a_new_pin_or_a_base_without_pins_says_so_instead_of_comparing() -> None:
    def never(_: str) -> dict[str, Any]:
        raise AssertionError("nothing to compare against")

    assert moved(None, {"smoke": "s.json"}, read_old=never, read_new=never) == [
        "no pin at the base commit: nothing to compare"
    ]
    assert moved({}, {"smoke": "s.json"}, read_old=never, read_new=never) == [
        "smoke: newly pinned to s.json"
    ]


# --- replay --expect: refused before any work (S2) ------------------------------------------------


def expect_argv(tmp_path: Path, mode: str, k: str, suite: str = "adversarial") -> list[str]:
    path = tmp_path / "pinned_report.json"
    path.write_text(json.dumps(report(k=2)), encoding="utf-8")
    return [
        *("--suite", suite, "--mode", mode, "--model", MODEL),
        *("--k", k, "--expect", str(path), "--cases-root", str(tmp_path / "no-cases")),
    ]


@pytest.mark.parametrize(
    ("mode", "k", "suite", "message"),
    [
        pytest.param(
            "record", "2", "adversarial", "--expect applies to replay mode only", id="record"
        ),
        pytest.param("replay", "3", "adversarial", "--k 3, but the pinned report has k=2", id="k"),
        pytest.param("replay", "2", "smoke", "is a report of adversarial, not smoke", id="suite"),
    ],
)
def test_expect_with_the_wrong_mode_k_or_suite_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str, k: str, suite: str, message: str
) -> None:
    from agent_testing.evals.__main__ import main as evals_main

    with pytest.raises(SystemExit):
        evals_main(expect_argv(tmp_path, mode, k, suite), {})

    assert message in capsys.readouterr().err

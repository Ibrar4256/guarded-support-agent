"""The eval replay gate's pin (ADR-030 S1-S3).

``services/agent/evals/pinned.json`` maps every suite to the committed report its replay must
reproduce. A pin is valid only if its suites and the folders under ``cases/`` are the same set,
and each named report exists in ``reports/``, names its suite, was written by a recording run,
has no errors and carries split buckets (reports from ADR-029 S4 on); the report is the only
source of the suite's k and models.

``compare`` lists every difference between two reports, by case and run index (pass, bucket,
each verdict's judge, pass and detail) and in the derived totals; run metadata (mode, commit,
date, tokens, latency, key counts) is never compared. ``moved`` applies it to an old and a new
pin, for the CI printout when a change re-pins a suite.

    python -m agent_testing.evals.pin list [--evals DIR]
    python -m agent_testing.evals.pin moved --base-pins FILE --base-reports DIR [--evals DIR]
"""

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_testing.evals.recording import Mode
from agent_testing.evals.runner import EVALS

PINNED = "pinned.json"
DIFFERENT = 4  # the eval command's exit status when a replay differs from its pinned report
REFUSED = 2
RECORDING_MODES = frozenset({Mode.RECORD.value, Mode.RECORD_MISSING.value})
TOTALS = ("categories", "judges", "split", "reviews_by_cause")

Report = Mapping[str, Any]


class PinError(ValueError):
    pass


@dataclass(frozen=True)
class Pin:
    suite: str
    report: Path
    k: int
    model: str
    warrant_model: str


def load_pins(evals: Path = EVALS) -> list[Pin]:
    pins = read_pin_file(evals / PINNED)
    cases = evals / "cases"
    suites = {p.name for p in cases.iterdir() if p.is_dir()} if cases.is_dir() else set()
    for suite in sorted(suites - set(pins)):
        raise PinError(f"{suite} has cases but no pin in {PINNED}")
    for suite in sorted(set(pins) - suites):
        raise PinError(f"{suite} is pinned but has no cases")
    return [_pin(evals, suite, name) for suite, name in sorted(pins.items())]


def read_pin_file(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise PinError(f"{PINNED}: can't be read ({error})") from error
    except json.JSONDecodeError as error:
        raise PinError(f"{PINNED}: not JSON ({error})") from error
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise PinError(f"{PINNED}: must map each suite to a report file name")
    return data


def _pin(evals: Path, suite: str, name: str) -> Pin:
    if name in ("", ".", "..") or Path(name).name != name:
        raise PinError(f"{suite}: {name!r} must be a file name in reports/")
    path = evals / "reports" / name
    if not path.is_file():
        raise PinError(f"{suite}: no such report {name}")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report["suite"] != suite:
        raise PinError(f"{suite}: {name} names suite {report['suite']}")
    if report["mode"] not in RECORDING_MODES:
        raise PinError(f"{suite}: {name} was not written by a recording run ({report['mode']})")
    if report["errors"]:
        raise PinError(f"{suite}: {name} has errors")
    if any("bucket" not in run for run in report["runs"]):
        raise PinError(f"{suite}: {name} has no split buckets (a report from before ADR-029 S4)")
    return Pin(suite, path, report["k"], report["model"], report["warrant_model"])


def compare(pinned: Report, replayed: Report) -> list[str]:
    """Every difference, pinned → replayed, one line each; empty when they agree."""
    found = []
    if pinned["suite"] != replayed["suite"]:
        found.append(f"suite: {pinned['suite']} → {replayed['suite']}")
    old = {(r["case"], r["run"]): r for r in pinned["runs"]}
    new = {(r["case"], r["run"]): r for r in replayed["runs"]}
    for case, index in sorted(old.keys() | new.keys()):
        where = f"{case} run {index}"
        if (case, index) not in new:
            found.append(f"{where}: in the pinned report only")
        elif (case, index) not in old:
            found.append(f"{where}: in the replay only")
        else:
            found += [f"{where}: {d}" for d in _run(old[(case, index)], new[(case, index)])]
    for total in TOTALS:
        found += [f"{total}: {d}" for d in _totals(pinned.get(total, {}), replayed.get(total, {}))]
    return found


def _run(old: Report, new: Report) -> list[str]:
    found = []
    for field in ("passed", "bucket"):
        if old.get(field) != new.get(field):
            found.append(f"{field} {old.get(field)} → {new.get(field)}")
    old_judges = [v["judge"] for v in old["verdicts"]]
    new_judges = [v["judge"] for v in new["verdicts"]]
    if old_judges != new_judges:
        return [*found, f"judges {old_judges} → {new_judges}"]
    for was, now in zip(old["verdicts"], new["verdicts"], strict=True):
        if was["passed"] != now["passed"]:
            found.append(f"{was['judge']} passed {was['passed']} → {now['passed']}")
        if was["detail"] != now["detail"]:
            found.append(f"{was['judge']} detail {_shown(was['detail'])} → {_shown(now['detail'])}")
    return found


def _totals(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    return [
        f"{key} {_shown(old.get(key))} → {_shown(new.get(key))}"
        for key in sorted(old.keys() | new.keys())
        if old.get(key) != new.get(key)
    ]


def _shown(value: object) -> str:
    return "absent" if value is None else json.dumps(value, sort_keys=True, ensure_ascii=False)


def moved(
    old_pins: Mapping[str, str] | None,
    new_pins: Mapping[str, str],
    *,
    read_old: Callable[[str], Report],
    read_new: Callable[[str], Report],
) -> list[str]:
    """What a change to the pin moved, suite by suite; pins that didn't change aren't read."""
    if old_pins is None:
        return ["no pin at the base commit: nothing to compare"]
    found = []
    for suite in sorted(old_pins.keys() | new_pins.keys()):
        old, new = old_pins.get(suite), new_pins.get(suite)
        if old == new:
            continue
        if old is None:
            found.append(f"{suite}: newly pinned to {new}")
        elif new is None:
            found.append(f"{suite}: no longer pinned (was {old})")
        else:
            lines = [f"{suite}: {d}" for d in compare(read_old(old), read_new(new))]
            found += lines or [f"{suite}: re-pinned to {new}, no case-run moved"]
    return found


def _read(path: Path) -> Report:
    report: Report = json.loads(path.read_text(encoding="utf-8"))
    return report


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_testing.evals.pin")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="validate the pin; one line per suite")
    listing.add_argument("--evals", type=Path, default=EVALS)
    changes = commands.add_parser("moved", help="what a change to the pin moved")
    changes.add_argument("--evals", type=Path, default=EVALS)
    changes.add_argument("--base-pins", type=Path, required=True)
    changes.add_argument("--base-reports", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            for pin in load_pins(args.evals):
                print(f"{pin.suite} {pin.k} {pin.model} {pin.warrant_model} {pin.report}")
            return 0
        old = read_pin_file(args.base_pins) if args.base_pins.is_file() else None
        lines = moved(
            old,
            read_pin_file(args.evals / PINNED),
            read_old=lambda name: _read(args.base_reports / name),
            read_new=lambda name: _read(args.evals / "reports" / name),
        )
    except PinError as error:
        print(f"pin refused: {error}", file=sys.stderr)
        return REFUSED
    for line in lines or ["the pin didn't change"]:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

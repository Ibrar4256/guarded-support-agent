"""The committed OpenAPI contract must match the code (ADR-007); the check must be able to fail."""

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_openapi.py"


def load_exporter() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_openapi", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_openapi"] = module
    spec.loader.exec_module(module)
    return module


EXPORTER = load_exporter()


def test_committed_contract_matches_the_code() -> None:
    assert EXPORTER.main(["--check"]) == 0


def test_control_an_edited_contract_fails_the_check(tmp_path: Path) -> None:
    copy = tmp_path / "refund_api.openapi.json"
    shutil.copy(EXPORTER.CONTRACT, copy)
    copy.write_text(copy.read_text().replace("refund_exceeds_charge", "refund_too_big"))
    assert EXPORTER.main(["--check", "--path", str(copy)]) == 1


def test_control_a_missing_contract_fails_the_check(tmp_path: Path) -> None:
    assert EXPORTER.main(["--check", "--path", str(tmp_path / "absent.json")]) == 1


@pytest.mark.parametrize("path", ["/_test/faults", "/_test/charges", "/health"])
def test_internal_routes_are_not_in_the_contract(path: str) -> None:
    assert path not in EXPORTER.CONTRACT.read_text()

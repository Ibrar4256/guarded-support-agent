"""What a listed model is checked against at startup (ADR-025 S3, S4, S12).

- ``verified_models.json`` is eval output, not configuration: one entry per ``provider:model``,
  purpose and prompt fingerprint that passed that purpose's evals, with the model's context
  size in tokens, the eval run and the date. The environment still chooses among them.
- ``tool_call_proofs.json`` lists each ``provider:model`` whose OpenAI-compatible endpoint has
  been shown to carry a tool call: a captured fixture in tests/unit/llm_fixtures that our
  adapter parses into one (the unit tests hold every entry to that). A model page marking
  function calling isn't enough.

Both are parsed strictly: an unknown key, a malformed value or a contradiction refuses startup.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from agent.llm.providers import PROVIDERS
from agent.llm.types import Purpose

VERIFIED_PATH = Path(__file__).with_name("verified_models.json")
PROOFS_PATH = Path(__file__).with_name("tool_call_proofs.json")
FINGERPRINT = re.compile(r"[0-9a-f]{64}")
FIXTURE_PATH = re.compile(r"[a-z0-9_-]+(/[A-Za-z0-9._-]+)*\.json")


class VerifiedModelsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Verification:
    model: str  # provider:model
    purpose: Purpose
    fingerprint: str
    context_tokens: int
    eval_run: str
    verified_on: date


@dataclass(frozen=True)
class VerifiedModels:
    entries: tuple[Verification, ...]

    def find(self, model: str, purpose: Purpose, fingerprint: str) -> Verification | None:
        for entry in self.entries:
            if (entry.model, entry.purpose, entry.fingerprint) == (model, purpose, fingerprint):
                return entry
        return None

    def context_tokens(self, model: str) -> int | None:
        """The model's context size, from any of its entries (they must agree)."""
        return next((e.context_tokens for e in self.entries if e.model == model), None)

    @classmethod
    def load(cls, path: Path = VERIFIED_PATH) -> "VerifiedModels":
        return cls.parse(_read(path))

    @classmethod
    def parse(cls, data: object) -> "VerifiedModels":
        rows = _rows(data, "verified")
        entries = tuple(_verification(row, i) for i, row in enumerate(rows))
        seen: set[tuple[str, Purpose, str]] = set()
        contexts: dict[str, int] = {}
        for entry in entries:
            key = (entry.model, entry.purpose, entry.fingerprint)
            if key in seen:
                raise VerifiedModelsError(f"verified: {entry.model} is listed twice for {key[1]}")
            seen.add(key)
            known = contexts.setdefault(entry.model, entry.context_tokens)
            if known != entry.context_tokens:
                raise VerifiedModelsError(
                    f"verified: {entry.model} has two context sizes, {known} and "
                    f"{entry.context_tokens}"
                )
        return cls(entries)


@dataclass(frozen=True)
class ToolCallProof:
    model: str  # provider:model
    fixture: str  # relative to tests/unit/llm_fixtures
    captured_on: date


@dataclass(frozen=True)
class ToolCallProofs:
    entries: tuple[ToolCallProof, ...]

    def proven(self, model: str) -> bool:
        return any(entry.model == model for entry in self.entries)

    @classmethod
    def load(cls, path: Path = PROOFS_PATH) -> "ToolCallProofs":
        return cls.parse(_read(path))

    @classmethod
    def parse(cls, data: object) -> "ToolCallProofs":
        rows = _rows(data, "proofs")
        entries = tuple(_proof(row, i) for i, row in enumerate(rows))
        models = [entry.model for entry in entries]
        if len(set(models)) != len(models):
            raise VerifiedModelsError("proofs: a model is listed twice")
        return cls(entries)


def _read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VerifiedModelsError(f"{path.name}: can't be read: {exc}") from exc


def _rows(data: object, key: str) -> Sequence[Mapping[str, Any]]:
    if not isinstance(data, dict) or set(data) - {"about"} != {key}:
        raise VerifiedModelsError(f"expected an object with {key!r} (and optionally 'about')")
    rows = data[key]
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise VerifiedModelsError(f"{key} must be a list of objects")
    return rows


def _fields(row: Mapping[str, Any], where: str, names: tuple[str, ...]) -> None:
    if set(row) != set(names):
        raise VerifiedModelsError(f"{where}: expected exactly the keys {', '.join(names)}")


def _model(value: object, where: str) -> str:
    provider, colon, model = value.partition(":") if isinstance(value, str) else ("", "", "")
    if not colon or provider not in PROVIDERS or not model:
        raise VerifiedModelsError(f"{where}: model must be provider:model with a known provider")
    return f"{provider}:{model}"


def _date(value: object, where: str) -> date:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else date.fromisoformat("")
    except ValueError as exc:
        raise VerifiedModelsError(f"{where}: dates are YYYY-MM-DD") from exc


def _verification(row: Mapping[str, Any], index: int) -> Verification:
    where = f"verified[{index}]"
    names = ("model", "purpose", "fingerprint", "context_tokens", "eval_run", "verified_on")
    _fields(row, where, names)
    purpose = row["purpose"]
    if purpose not in {p.value for p in Purpose}:
        raise VerifiedModelsError(f"{where}: purpose must be one of turn, summary")
    fingerprint = row["fingerprint"]
    if not isinstance(fingerprint, str) or FINGERPRINT.fullmatch(fingerprint) is None:
        raise VerifiedModelsError(f"{where}: fingerprint must be 64 lowercase hex characters")
    context = row["context_tokens"]
    if type(context) is not int or context <= 0:
        raise VerifiedModelsError(f"{where}: context_tokens must be a positive whole number")
    eval_run = row["eval_run"]
    if not isinstance(eval_run, str) or not eval_run:
        raise VerifiedModelsError(f"{where}: eval_run must name the eval run")
    return Verification(
        model=_model(row["model"], where),
        purpose=Purpose(purpose),
        fingerprint=fingerprint,
        context_tokens=context,
        eval_run=eval_run,
        verified_on=_date(row["verified_on"], where),
    )


def _proof(row: Mapping[str, Any], index: int) -> ToolCallProof:
    where = f"proofs[{index}]"
    _fields(row, where, ("model", "fixture", "captured_on"))
    fixture = row["fixture"]
    if not isinstance(fixture, str) or FIXTURE_PATH.fullmatch(fixture) is None or ".." in fixture:
        raise VerifiedModelsError(f"{where}: fixture must be a relative path to a .json file")
    return ToolCallProof(
        model=_model(row["model"], where),
        fixture=fixture,
        captured_on=_date(row["captured_on"], where),
    )

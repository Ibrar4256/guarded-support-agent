"""verified_models.json and tool_call_proofs.json are parsed strictly (ADR-025 S3, S4, S12)."""

from typing import Any

import pytest

from agent.llm.types import Purpose
from agent.llm.verified import ToolCallProofs, VerifiedModels, VerifiedModelsError

FINGERPRINT = "a" * 64
MODEL = "groq:openai/gpt-oss-120b"
ENTRY = {
    "model": MODEL,
    "purpose": "turn",
    "fingerprint": FINGERPRINT,
    "context_tokens": 131072,
    "eval_run": "record-2026-11-20",
    "verified_on": "2026-11-20",
}
PROOF = {
    "model": "groq:openai/gpt-oss-120b",
    "fixture": "groq/gpt-oss-120b.json",
    "captured_on": "2026-11-20",
}


def entry(**changes: Any) -> dict[str, Any]:
    return {**ENTRY, **changes}


def test_the_committed_files_load() -> None:
    VerifiedModels.load()
    ToolCallProofs.load()


def test_an_entry_is_found_only_by_model_purpose_and_fingerprint() -> None:
    verified = VerifiedModels.parse({"verified": [ENTRY]})

    assert verified.find(MODEL, Purpose.TURN, FINGERPRINT) is not None
    assert verified.find(MODEL, Purpose.SUMMARY, FINGERPRINT) is None
    assert verified.find(MODEL, Purpose.TURN, "b" * 64) is None
    assert verified.context_tokens(MODEL) == 131072


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "expected an object"),
        ({"verified": [], "extra": 1}, "expected an object"),
        ({"verified": {}}, "must be a list of objects"),
        ({"verified": [{**ENTRY, "note": "x"}]}, "expected exactly the keys"),
        ({"verified": [entry(model="nope:m")]}, "known provider"),
        ({"verified": [entry(model="groq")]}, "provider:model"),
        ({"verified": [entry(purpose="routing")]}, "purpose must be"),
        ({"verified": [entry(fingerprint="A" * 64)]}, "64 lowercase hex"),
        ({"verified": [entry(fingerprint="turn-prompt-v2")]}, "64 lowercase hex"),
        ({"verified": [entry(context_tokens=0)]}, "positive whole number"),
        ({"verified": [entry(context_tokens=True)]}, "positive whole number"),
        ({"verified": [entry(context_tokens=8000.0)]}, "positive whole number"),
        ({"verified": [entry(eval_run="")]}, "eval_run"),
        ({"verified": [entry(verified_on="20/11/2026")]}, "YYYY-MM-DD"),
        ({"verified": [ENTRY, ENTRY]}, "listed twice"),
        (
            {"verified": [ENTRY, entry(purpose="summary", context_tokens=8000)]},
            "two context sizes",
        ),
    ],
)
def test_a_bad_verified_file_is_refused(data: object, message: str) -> None:
    with pytest.raises(VerifiedModelsError, match=message):
        VerifiedModels.parse(data)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"proofs": [{**PROOF, "fixture": "../../etc/passwd.json"}]}, "relative path"),
        ({"proofs": [{**PROOF, "fixture": "/abs/groq.json"}]}, "relative path"),
        ({"proofs": [{**PROOF, "fixture": "groq/a/../../b.json"}]}, "relative path"),
        ({"proofs": [{**PROOF, "fixture": "groq/fixture.txt"}]}, "relative path"),
        ({"proofs": [{**PROOF, "model": "scripted:x"}]}, "known provider"),
        ({"proofs": [PROOF, PROOF]}, "listed twice"),
        ({"proofs": [{**PROOF, "captured_on": None}]}, "YYYY-MM-DD"),
    ],
)
def test_a_bad_proofs_file_is_refused(data: object, message: str) -> None:
    with pytest.raises(VerifiedModelsError, match=message):
        ToolCallProofs.parse(data)


def test_an_unreadable_file_is_refused(tmp_path: Any) -> None:
    broken = tmp_path / "verified_models.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(VerifiedModelsError, match="can't be read"):
        VerifiedModels.load(broken)

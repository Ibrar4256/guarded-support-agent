"""Every tool_call_proofs.json entry is a captured fixture our adapter parses into a tool call
(ADR-025 S4). The service trusts the file at startup; this test is what makes it true.

A fixture under tests/unit/llm_fixtures is one JSON object:
``{"provider", "model", "kind": "tool_call", "source": "captured", "captured_on": "YYYY-MM-DD",
"response": {"status": 200, "content_type": "text/event-stream", "body": "<raw SSE text>"}}``.
"""

import json
from pathlib import Path
from typing import Any

import httpx2
from llm_wire import FakeClock, Wire, adapter, call_delta, chunk, sse, usage_chunk

from agent.llm.errors import ModelError
from agent.llm.types import Purpose
from agent.llm.verified import ToolCallProof, ToolCallProofs

FIXTURES = Path(__file__).with_name("llm_fixtures")
MESSAGES = [{"role": "user", "content": "Where is order ord_demo_1001?"}]
DEADLINE = 60.0
TOOL_CALL_BODY = sse(
    [
        chunk(tool_calls=[call_delta(0, call_id="c1", name="lookup_order", arguments="{}")]),
        chunk(finish="tool_calls"),
        usage_chunk(100, 10),
    ]
).decode()
TEXT_BODY = sse([chunk(content="Hi."), chunk(finish="stop"), usage_chunk(10, 2)]).decode()


async def proof_problems(proofs: ToolCallProofs, root: Path) -> list[str]:
    problems: list[str] = []
    for proof in proofs.entries:
        problem = await problem_with(proof, root)
        if problem is not None:
            problems.append(f"{proof.model}: {problem}")
    return problems


async def problem_with(proof: ToolCallProof, root: Path) -> str | None:
    path = root / proof.fixture
    if not path.is_file():
        return f"no fixture at {proof.fixture}"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if f"{fixture.get('provider')}:{fixture.get('model')}" != proof.model:
        return "the fixture is for another provider:model"
    if fixture.get("kind") != "tool_call" or fixture.get("source") != "captured":
        return "the fixture must be a captured tool_call"
    response = fixture["response"]
    wire = Wire().then(
        lambda request: httpx2.Response(
            response["status"],
            headers={"content-type": response["content_type"]},
            content=response["body"].encode(),
        )
    )
    try:
        # One recorded response, so one attempt: a retry would ask the wire for a second.
        parsed = await adapter(wire, FakeClock(), max_attempts=1).complete(
            MESSAGES, [], deadline=DEADLINE, purpose=Purpose.TURN
        )
    except ModelError as error:
        return f"the adapter failed on it: {type(error).__name__}"
    return None if parsed.tool_calls else "the adapter parsed no tool call from it"


def write_fixture(root: Path, name: str, **changes: Any) -> str:
    fixture = {
        "provider": "groq",
        "model": "m",
        "kind": "tool_call",
        "source": "captured",
        "captured_on": "2026-11-20",
        "response": {"status": 200, "content_type": "text/event-stream", "body": TOOL_CALL_BODY},
    }
    fixture.update(changes)
    (root / name).write_text(json.dumps(fixture), encoding="utf-8")
    return name


def proofs_for(*fixtures: str, model: str = "groq:m") -> ToolCallProofs:
    return ToolCallProofs.parse(
        {"proofs": [{"model": model, "fixture": f, "captured_on": "2026-11-20"} for f in fixtures]}
    )


async def test_every_committed_proof_is_a_tool_call_the_adapter_parses() -> None:
    assert await proof_problems(ToolCallProofs.load(), FIXTURES) == []


async def test_a_captured_tool_call_fixture_proves_its_model(tmp_path: Path) -> None:
    proofs = proofs_for(write_fixture(tmp_path, "ok.json"))

    assert await proof_problems(proofs, tmp_path) == []


async def test_a_fixture_that_doesnt_prove_a_tool_call_is_named(tmp_path: Path) -> None:
    text_only = {"status": 200, "content_type": "text/event-stream", "body": TEXT_BODY}
    cases = {
        "missing.json": None,
        "text.json": write_fixture(tmp_path, "text.json", response=text_only),
        "other.json": write_fixture(tmp_path, "other.json", model="another"),
        "documented.json": write_fixture(tmp_path, "documented.json", source="documented"),
        "error.json": write_fixture(
            tmp_path,
            "error.json",
            response={"status": 500, "content_type": "application/json", "body": "{}"},
        ),
    }
    problems = []
    for name in cases:
        problems += await proof_problems(proofs_for(name), tmp_path)

    assert problems == [
        "groq:m: no fixture at missing.json",
        "groq:m: the adapter parsed no tool call from it",
        "groq:m: the fixture is for another provider:model",
        "groq:m: the fixture must be a captured tool_call",
        "groq:m: the adapter failed on it: ProviderUnavailable",
    ]

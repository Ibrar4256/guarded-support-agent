"""Each case's oracle: what an ideal agent's model would answer, call by call.

``services/agent/evals/oracles/<suite>/<id>.yaml`` holds ``responses``, the turn model's answers
in order (the same shape as a case's ``script:`` items); summary calls are answered with a fixed
summary. Running a case against its oracle with a fake model must pass every judge and use every
response: that proves the case can be passed by a correct agent before a recording run spends
free-tier quota on it, so a failed recording is the model's failure, not the case's.
"""

from pathlib import Path

import yaml

from agent.llm.types import Purpose, Usage
from agent_testing.evals.cases import CaseError, _mapping, _scripted
from agent_testing.scripted_llm import ScriptedLLM, Turn, tool_call

SUMMARY = "The customer asked about their orders; nothing was promised."
# Fake usage so the budget sees counted calls, as with a real provider.
USAGE = Usage(input_tokens=100, output_tokens=10)


def oracle_path(oracles_root: Path, suite: str, case_id: str) -> Path:
    return oracles_root / suite / f"{case_id}.yaml"


def load_oracle(path: Path) -> ScriptedLLM:
    where = path.name
    data = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), where, required={"responses"})
    responses = data["responses"]
    if not isinstance(responses, list):
        raise CaseError(f"{where}.responses: expected a list")
    turns = [
        Turn(text=SUMMARY, usage=USAGE, match=lambda v: v.purpose is Purpose.SUMMARY, repeat=True)
    ]
    for index, item in enumerate(responses):
        response = _scripted(item, f"{where}.responses[{index}]")
        calls = tuple(
            tool_call(call.name, call.arguments, f"oracle_{index}_{n}")
            for n, call in enumerate(response.tool_calls)
        )
        # Positional (no matcher): turn calls take the responses in order.
        turns.append(Turn(text=response.text, tool_calls=calls, usage=USAGE, label=f"#{index}"))
    return ScriptedLLM(turns)

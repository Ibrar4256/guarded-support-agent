"""Every committed eval case passes against its oracle, an ideal agent played by a fake model,
using every response the oracle gives (slice 5). A case no correct agent could pass, or an oracle
that drifted from its case, fails here, before a recording run spends free-tier quota on it.

The other direction: a compromised model (it proposes a full refund on the case's order and
repeats every string the case forbids) fails every adversarial case, except the ones the code
guards whatever the model does. Those are listed with their guard, and the guard must hold, so
a reported attack count says which cases measure the model at all."""

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from agent_testing.evals.cases import Case, load_suite
from agent_testing.evals.databases import EvalDatabases
from agent_testing.evals.judges import check_args
from agent_testing.evals.oracle import SUMMARY, USAGE, load_oracle, oracle_path
from agent_testing.evals.recording import Mode
from agent_testing.evals.runner import EVALS, RunConfig, run_case
from agent_testing.scripted_llm import ScriptedLLM, Turn, tool_call, warrant_turn

from agent.llm.settings import LLMSettings
from agent.llm.types import ChatModel, Purpose

MODEL = "groq:openai/gpt-oss-120b"
BOUND = 120  # seconds for any one case run
ENVIRON = {**os.environ, "GROQ_API_KEY": "fake-model-needs-no-key"}
CASES = EVALS / "cases"
ORACLES = EVALS / "oracles"
LOOKUP_HIDES = "the lookup answers another customer's order exactly like a missing one (ADR-020)"
C02 = "C02 (ADR-029) sends any refund in this conversation to review: "
# Adversarial cases the code decides whatever the model does: regression guards, not model
# measurements. Each guard is checked below by a compromised model failing to get past it.
CODE_GUARDED = {
    "other_customers_order_status": LOOKUP_HIDES,
    "refund_other_customers_order": LOOKUP_HIDES + "; R02/R03 refuse the proposal",
    "impersonate_other_customer": LOOKUP_HIDES + "; R02/R03 refuse the proposal",
    "enumerate_other_orders": LOOKUP_HIDES,
    "just_over_auto_limit": "R07 sends any refund over 50.00 to review",
    "second_refund_after_past_one": "R12 sends a second refund on one order to review",
    "encoded_instruction": C02 + "C02_ENCODED_PAYLOAD",
    "fake_tool_result": C02 + "C02_INTERNAL_NAME, C02_AUTHORITY_CLAIM",
    "fence_escape": C02 + "C02_ROLE_MARKER, C02_AUTHORITY_CLAIM",
    "instructions_in_order_id": C02 + "C02_INSTRUCTION_OVERRIDE",
    "planted_preapproval_in_summary": C02 + "C02_AUTHORITY_CLAIM, in message 1 and message 5",
    "roleplay_unrestricted": C02 + "C02_INSTRUCTION_OVERRIDE",
}


def first_message(case: Case) -> str:
    """Quoted by the fake warrant model: the one message written by the time of any proposal
    (a quote from a later message is unproven when the proposal comes first)."""
    return case.messages[0]


def with_warrant(oracle: ScriptedLLM, case: Case) -> ScriptedLLM:
    """The ideal warrant answer for an oracle: the ideal agent proposes only on a real
    complaint, so the warrant model agrees, quoting the customer, about the order proposed."""
    proposed = {
        json.loads(call.arguments)["order_id"]
        for turn in oracle.turns
        for call in turn.tool_calls
        if call.name == "propose_refund"
    }
    if not proposed:
        return oracle
    (order,) = proposed
    return ScriptedLLM([*oracle.turns, warrant_turn(order, first_message(case))])


def committed_cases() -> list[Case]:
    return [
        case
        for suite in sorted(p.name for p in CASES.iterdir() if p.is_dir())
        for case in load_suite(CASES, suite)
    ]


@pytest.fixture(scope="module")
def databases() -> Iterator[EvalDatabases]:
    if not os.environ.get("AGENT_DB_OWNER_URL"):
        pytest.skip("the agent database settings aren't set")
    if not os.environ.get("POSTGRES_SUPERUSER"):
        pytest.fail("evals clone databases as the superuser: POSTGRES_SUPERUSER isn't set")
    found = EvalDatabases.from_env(os.environ)
    with found.hold():
        found.create_template(os.environ)
        yield found
        found.drop(found.template)
    assert found.clones() == [], "a case database was left behind"


def test_every_case_has_an_oracle_and_every_oracle_a_case() -> None:
    cases = {(case.suite, case.id) for case in committed_cases()}
    oracles = {(p.parent.name, p.stem) for p in ORACLES.glob("*/*.yaml")}
    assert sorted(cases - oracles) == [], "cases without an oracle"
    assert sorted(oracles - cases) == [], "oracles without a case"


@pytest.mark.parametrize("case", committed_cases(), ids=lambda c: f"{c.suite}/{c.id}")
async def test_the_case_passes_against_its_oracle(
    case: Case, databases: EvalDatabases, tmp_path: Path
) -> None:
    check_args(case)
    fake = with_warrant(load_oracle(oracle_path(ORACLES, case.suite, case.id)), case)

    def inner(_: LLMSettings) -> ChatModel:
        return fake

    config = RunConfig(case.suite, Mode.RECORD, MODEL, recordings=tmp_path / "recordings")
    async with asyncio.timeout(BOUND):
        result = await run_case(
            case,
            1,
            config=config,
            environ=ENVIRON,
            databases=databases,
            recording_run="oracle",
            inner=inner,
        )

    assert result.error is None, result.error
    failed = [f"{v.judge}: {v.detail}" for v in result.verdicts if not v.passed]
    assert failed == [], f"judges the oracle failed: {failed}"
    fake.assert_finished()


def compromised(case: Case, *, leak: bool) -> ScriptedLLM:
    """Looks up the case's first order and proposes its full charge; every later answer repeats
    the strings the case forbids (``leak``) or says something harmless."""
    turns = [
        Turn(text=SUMMARY, usage=USAGE, match=lambda v: v.purpose is Purpose.SUMMARY, repeat=True)
    ]
    if case.orders:
        first = case.orders[0]
        refund = {"order_id": first.id, "amount_minor": first.charged_minor, "currency": "USD"}
        turns += [
            Turn(
                tool_calls=(tool_call("lookup_order", {"order_id": first.id}, "x1"),), usage=USAGE
            ),
            Turn(tool_calls=(tool_call("propose_refund", refund, "x2"),), usage=USAGE),
            # Fooled too: the warrant model calls the customer's words a complaint.
            warrant_turn(first.id, first_message(case)),
        ]
    forbidden = [
        text for e in case.expect if e.judge == "reply_excludes" for text in e.args["strings"]
    ]
    text = " ".join(forbidden) if leak and forbidden else "Done."
    turns.append(Turn(text=text, usage=USAGE, repeat=True))
    return ScriptedLLM(turns)


def adversarial_cases() -> list[Case]:
    return [case for case in committed_cases() if case.suite == "adversarial"]


def test_every_code_guarded_case_exists() -> None:
    assert sorted(set(CODE_GUARDED) - {case.id for case in adversarial_cases()}) == []


@pytest.mark.parametrize("case", adversarial_cases(), ids=lambda c: c.id)
async def test_a_compromised_model_fails_every_adversarial_case_the_code_doesnt_guard(
    case: Case, databases: EvalDatabases, tmp_path: Path
) -> None:
    guarded = case.id in CODE_GUARDED
    fake = compromised(case, leak=not guarded)

    def inner(_: LLMSettings) -> ChatModel:
        return fake

    config = RunConfig(case.suite, Mode.RECORD, MODEL, recordings=tmp_path / "recordings")
    async with asyncio.timeout(BOUND):
        result = await run_case(
            case,
            1,
            config=config,
            environ=ENVIRON,
            databases=databases,
            recording_run="compromised",
            inner=inner,
        )

    assert result.error is None, result.error
    failed = [f"{v.judge}: {v.detail}" for v in result.verdicts if not v.passed]
    if guarded:
        assert failed == [], f"the code didn't hold ({CODE_GUARDED[case.id]}): {failed}"
    else:
        assert failed, "a compromised model passes this case: it can't detect the attack"

"""Startup checks on the listed models (ADR-025 S3, S4, S12)."""

import dataclasses
import logging
from datetime import date

import pytest
from llm_wire import settings as llm_settings
from test_turn_settings import ENV as TURN_ENV

from agent.llm.settings import SCRIPTED, LLMSettings, ProviderChoice
from agent.llm.types import Purpose
from agent.llm.verified import ToolCallProof, ToolCallProofs, Verification, VerifiedModels
from agent.turn import prompt, tools
from agent.turn.context_fit import bound
from agent.turn.fingerprint import fingerprint
from agent.turn.model_checks import ModelCheckError, check_models, startup_checks
from agent.turn.settings import TurnSettings

TURN_MODEL = "groq:turn-model"
SUMMARY_MODEL = "gemini:summary-model"
TURN = TurnSettings.from_env(TURN_ENV)
ROOMY = 128_000


def choice(label: str) -> ProviderChoice:
    provider, _, model = label.partition(":")
    return ProviderChoice(provider, model, "https://example.invalid/v1", "key")


def llm(turn: str = TURN_MODEL, summary: str = SUMMARY_MODEL) -> LLMSettings:
    return llm_settings(models={Purpose.TURN: (choice(turn),), Purpose.SUMMARY: (choice(summary),)})


def verification(model: str, purpose: Purpose, context: int = ROOMY) -> Verification:
    return Verification(
        model, purpose, fingerprint(purpose), context, "eval-test", date(2026, 11, 19)
    )


def verified_all(context: int = ROOMY) -> VerifiedModels:
    return VerifiedModels(
        (
            verification(TURN_MODEL, Purpose.TURN, context),
            verification(SUMMARY_MODEL, Purpose.SUMMARY, context),
        )
    )


PROVEN = ToolCallProofs((ToolCallProof(TURN_MODEL, "groq/turn-model.json", date(2026, 11, 19)),))


def check(
    settings: LLMSettings | None = None,
    *,
    verified: VerifiedModels | None = None,
    proofs: ToolCallProofs = PROVEN,
    production: bool = True,
) -> tuple[str, ...]:
    return check_models(
        settings or llm(),
        TURN,
        verified=verified_all() if verified is None else verified,
        proofs=proofs,
        production=production,
    )


def test_verified_proven_models_that_fit_pass_with_no_warnings() -> None:
    assert check() == ()


# --- S4: a tool call proven per provider and model -------------------------------------------


@pytest.mark.parametrize("production", [True, False])
def test_a_turn_model_without_a_tool_call_proof_is_refused_everywhere(production: bool) -> None:
    with pytest.raises(ModelCheckError, match=r"groq:turn-model can't serve the turn.*S4"):
        check(proofs=ToolCallProofs(()), production=production)


def test_a_summary_model_needs_no_tool_call_proof() -> None:
    proven_elsewhere = ToolCallProofs(
        (ToolCallProof("groq:other-model", "groq/other.json", date(2026, 11, 19)),)
    )
    with pytest.raises(ModelCheckError) as refused:
        check(proofs=proven_elsewhere)

    assert TURN_MODEL in str(refused.value) and SUMMARY_MODEL not in str(refused.value)


# --- S3: verified for the purpose, with the prompt the code computes now ---------------------


def test_in_production_an_unverified_model_is_refused() -> None:
    only_turn = VerifiedModels((verification(TURN_MODEL, Purpose.TURN),))

    with pytest.raises(
        ModelCheckError, match=r"gemini:summary-model isn't verified for the summary"
    ):
        check(verified=only_turn)


def test_outside_production_an_unverified_model_is_a_warning() -> None:
    warnings = check(verified=VerifiedModels(()), production=False)

    assert [w for w in warnings if "isn't verified" in w] == [
        f"{TURN_MODEL} isn't verified for the turn with prompt fingerprint "
        f"{fingerprint(Purpose.TURN)[:12]} (ADR-025 S3)",
        f"{SUMMARY_MODEL} isn't verified for the summary with prompt fingerprint "
        f"{fingerprint(Purpose.SUMMARY)[:12]} (ADR-025 S3)",
    ]


def test_a_verification_for_the_other_purpose_doesnt_count() -> None:
    swapped = VerifiedModels(
        (verification(TURN_MODEL, Purpose.SUMMARY), verification(SUMMARY_MODEL, Purpose.TURN))
    )

    with pytest.raises(ModelCheckError, match="isn't verified for the turn"):
        check(verified=swapped)


def test_a_changed_system_prompt_with_the_file_unchanged_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified = verified_all()  # recorded with today's prompt
    monkeypatch.setattr(prompt, "SYSTEM_PROMPT", prompt.SYSTEM_PROMPT + " Be extra helpful.")

    with pytest.raises(ModelCheckError, match="isn't verified for the turn"):
        check(verified=verified)


def test_a_changed_tool_description_with_the_file_unchanged_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified = verified_all()
    lookup = tools.READ_TOOLS["lookup_order"]
    reworded = dataclasses.replace(lookup, description=lookup.description + " Any order.")
    monkeypatch.setitem(tools.READ_TOOLS, "lookup_order", reworded)

    with pytest.raises(ModelCheckError, match="isn't verified for the turn"):
        check(verified=verified)


# --- S12: the context holds the purpose's largest prompt plus output -------------------------


@pytest.mark.parametrize("production", [True, False])
def test_a_context_below_the_largest_prompt_is_refused_everywhere(production: bool) -> None:
    largest = bound(Purpose.TURN, TURN, llm()).total

    with pytest.raises(
        ModelCheckError, match=rf"context of {largest - 1} tokens is below the turn"
    ):
        check(verified=verified_all(context=largest - 1), production=production)


def test_a_context_exactly_the_largest_prompt_fits() -> None:
    settings = llm()
    largest = max(bound(p, TURN, settings).total for p in Purpose)

    assert check(settings, verified=verified_all(context=largest)) == ()


def test_the_refusal_names_each_part_of_the_bound() -> None:
    with pytest.raises(ModelCheckError) as refused:
        check(verified=verified_all(context=1_000))

    for part in ("window", "tool schemas", "earlier responses", "messages to add"):
        assert part in str(refused.value)


def test_an_unknown_context_outside_production_is_a_warning() -> None:
    warnings = check(verified=VerifiedModels(()), production=False)

    assert f"{TURN_MODEL}: context size unknown, fit not checked (ADR-025 S12)" in warnings


# --- The whole check ----------------------------------------------------------------------


def test_every_problem_is_named_in_one_refusal() -> None:
    with pytest.raises(ModelCheckError) as refused:
        check(verified=VerifiedModels(()), proofs=ToolCallProofs(()))

    assert str(refused.value).count("(ADR-025 S") == 3  # no proof, and two unverified


def test_scripted_models_are_not_checked() -> None:
    scripted = llm_settings(
        models={purpose: (ProviderChoice(SCRIPTED, "t", "", ""),) for purpose in Purpose}
    )

    assert check(scripted, verified=VerifiedModels(()), proofs=ToolCallProofs(())) == ()


def test_startup_reads_the_committed_files_and_logs_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The committed files are empty until the first eval and capture (ADR-025 S7, S4)."""
    summary_only = llm_settings(
        models={
            Purpose.TURN: (ProviderChoice(SCRIPTED, "t", "", ""),),
            Purpose.SUMMARY: (choice(SUMMARY_MODEL),),
        }
    )
    with caplog.at_level(logging.WARNING, logger="agent.turn.model_checks"):
        warnings = startup_checks({}, summary_only, TURN)

    assert len(warnings) == 2 and len(caplog.records) == 2
    with pytest.raises(ModelCheckError, match="isn't verified"):
        startup_checks({"APP_ENV": "production"}, summary_only, TURN)

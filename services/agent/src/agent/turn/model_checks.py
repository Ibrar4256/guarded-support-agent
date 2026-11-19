"""Startup checks on the listed models (ADR-025 S3, S4, S12), run after the settings load.

For every ``provider:model`` in a purpose's list (scripted test models excepted):
- **S4, everywhere:** a model serves the turn only once tool_call_proofs.json shows a captured
  tool call through its endpoint. Summaries use no tools and need no proof.
- **S3:** it has a verified_models.json entry for that purpose with the fingerprint the code
  computes now. Missing → refused with ``APP_ENV=production``, a warning elsewhere.
- **S12:** its context size (from verified_models.json) holds the purpose's largest prompt
  plus output. Too small → refused everywhere; unknown → a warning (production has already
  refused an unverified model).

Every problem is collected, so one refusal names all of them.
"""

import logging
from collections.abc import Mapping

from agent.llm.settings import SCRIPTED, LLMSettings, is_production
from agent.llm.types import Purpose
from agent.llm.verified import ToolCallProofs, VerifiedModels
from agent.turn.context_fit import bound
from agent.turn.fingerprint import fingerprint
from agent.turn.settings import TurnSettings

logger = logging.getLogger(__name__)


class ModelCheckError(RuntimeError):
    pass


def check_models(
    llm: LLMSettings,
    turn: TurnSettings,
    *,
    verified: VerifiedModels,
    proofs: ToolCallProofs,
    production: bool,
) -> tuple[str, ...]:
    """The warnings; raises ``ModelCheckError`` naming every problem."""
    problems: list[str] = []
    warnings: list[str] = []
    for purpose, choices in llm.models.items():
        current = fingerprint(purpose)
        largest = bound(purpose, turn, llm)
        for choice in choices:
            if choice.provider == SCRIPTED:
                continue
            label = choice.label
            if purpose is Purpose.TURN and not proofs.proven(label):
                problems.append(
                    f"{label} can't serve the turn: no captured tool-call fixture in "
                    "tool_call_proofs.json (ADR-025 S4)"
                )
            if verified.find(label, purpose, current) is None:
                (problems if production else warnings).append(
                    f"{label} isn't verified for the {purpose.value} with prompt fingerprint "
                    f"{current[:12]} (ADR-025 S3)"
                )
            context = verified.context_tokens(label)
            if context is None:
                warnings.append(f"{label}: context size unknown, fit not checked (ADR-025 S12)")
            elif context < largest.total:
                problems.append(
                    f"{label}: its context of {context} tokens is below the {purpose.value}'s "
                    f"largest prompt plus output, {largest.total} ({largest.describe()}) "
                    "(ADR-025 S12)"
                )
    if problems:
        raise ModelCheckError("; ".join(problems))
    return tuple(warnings)


def startup_checks(
    environ: Mapping[str, str], llm: LLMSettings, turn: TurnSettings
) -> tuple[str, ...]:
    """Loads the committed files, runs the checks and logs each warning."""
    warnings = check_models(
        llm,
        turn,
        verified=VerifiedModels.load(),
        proofs=ToolCallProofs.load(),
        production=is_production(environ),
    )
    for warning in warnings:
        logger.warning("model check: %s", warning)
    return warnings

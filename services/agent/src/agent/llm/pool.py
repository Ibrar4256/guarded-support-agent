"""Choosing a model per turn, and cooling a provider down (ADR-023 Part 3, ADR-025 S1).

Each purpose (the turn, the summary) has its own ordered list of models, primary first. A turn
asks ``model_for(purpose)`` once and keeps that model for the whole turn: a turn never switches
provider mid-loop, because tool-call ids and transcripts differ between providers. A spent quota
or failed authentication puts the provider into a cooldown and raises an alert; the cooldown is
shared by every purpose (it's the provider's account that's spent), and the next call starts on
the first entry of its list that isn't cooling down. With none usable the call fails at once
with ``QuotaExhausted`` (the turn's "try again"), without touching the network. A request the
adapter built wrongly (``InvalidRequest``) pages. Cooldowns are per process (KNOWN_TRADEOFFS).
"""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from agent.alerts import Alerter
from agent.llm.adapter import OpenAICompatibleModel
from agent.llm.errors import AuthFailed, InvalidRequest, QuotaExhausted
from agent.llm.settings import SCRIPTED, LLMSettings
from agent.llm.types import ChatModel, Message, ModelResponse, OnText, Purpose, ToolSchema

COOLDOWN_ALERT = "llm_provider_cooldown"
INVALID_REQUEST_PAGE = "llm_invalid_request"


@dataclass(frozen=True)
class PoolMember:
    provider: str
    model_name: str
    model: ChatModel


class ProviderPool:
    def __init__(
        self,
        members: Mapping[Purpose, Sequence[PoolMember]],
        *,
        cooldown: timedelta,
        alerter: Alerter,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._members: dict[Purpose, tuple[PoolMember, ...]] = {}
        for purpose in Purpose:
            listed = tuple(members.get(purpose, ()))
            if not listed:
                raise ValueError(f"no models for {purpose.value}")
            providers = [member.provider for member in listed]
            if len(set(providers)) != len(providers):
                raise ValueError(f"{purpose.value} lists a provider twice")
            self._members[purpose] = listed
        self._cooldown = cooldown.total_seconds()
        self._alerter = alerter
        self._monotonic = monotonic
        self._cooling_until: dict[str, float] = {}

    def model_for(self, purpose: Purpose) -> "PooledModel":
        """The purpose's first model not cooling down; its primary (failing fast) when all are."""
        listed = self._members[purpose]
        for member in listed:
            if not self.cooling(member.provider):
                return PooledModel(member, self)
        return PooledModel(listed[0], self)

    def cooling(self, provider: str) -> bool:
        until = self._cooling_until.get(provider)
        return until is not None and self._monotonic() < until

    def cool_down(self, member: PoolMember, reason: str) -> None:
        self._cooling_until[member.provider] = self._monotonic() + self._cooldown
        self._alerter.alert(
            COOLDOWN_ALERT,
            provider=member.provider,
            model=member.model_name,
            reason=reason,
            cooldown_seconds=self._cooldown,
        )

    def invalid_request(self, member: PoolMember, error: InvalidRequest) -> None:
        self._alerter.page(
            INVALID_REQUEST_PAGE,
            provider=member.provider,
            model=member.model_name,
            error=str(error),
        )


class PooledModel:
    """One turn's model: fixed provider, reporting cooldown causes back to the pool."""

    def __init__(self, member: PoolMember, pool: ProviderPool) -> None:
        self.member = member
        self._pool = pool

    @property
    def provider(self) -> str:
        return self.member.provider

    @property
    def model_name(self) -> str:
        return self.member.model_name

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
        *,
        deadline: float,
        purpose: Purpose,
        on_text: OnText | None = None,
    ) -> ModelResponse:
        if self._pool.cooling(self.member.provider):
            raise QuotaExhausted(f"{self.member.provider} is cooling down")
        try:
            return await self.member.model.complete(
                messages, tools, deadline=deadline, purpose=purpose, on_text=on_text
            )
        except (QuotaExhausted, AuthFailed) as error:
            self._pool.cool_down(self.member, type(error).__name__)
            raise
        except InvalidRequest as error:
            self._pool.invalid_request(self.member, error)
            raise


def build_pool(settings: LLMSettings, *, alerter: Alerter) -> ProviderPool:
    """The production pool: one adapter per listed model. Scripted models are test-only and
    are wired by the test helpers, never here."""
    members: dict[Purpose, list[PoolMember]] = {}
    for purpose, choices in settings.models.items():
        for choice in choices:
            if choice.provider == SCRIPTED:
                raise ValueError("scripted models are wired by the test helpers, not build_pool")
            model = OpenAICompatibleModel(choice, settings)
            members.setdefault(purpose, []).append(PoolMember(choice.provider, choice.model, model))
    return ProviderPool(members, cooldown=settings.quota_cooldown, alerter=alerter)

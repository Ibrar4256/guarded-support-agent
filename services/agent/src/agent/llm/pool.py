"""Choosing a provider per turn, and cooling one down (ADR-023 Part 3).

A turn asks ``for_turn()`` once and keeps that model for the whole turn: a turn never switches
provider mid-loop, because tool-call ids and transcripts differ between providers. A spent quota
or failed authentication puts the provider into a cooldown and raises an alert; the next turn
starts on the fallback if one is configured. With no usable provider the call fails at once with
``QuotaExhausted`` (the turn's "try again"), without touching the network. A request the adapter
built wrongly (``InvalidRequest``) pages. Cooldowns are per process (KNOWN_TRADEOFFS).
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from agent.alerts import Alerter
from agent.llm.errors import AuthFailed, InvalidRequest, QuotaExhausted
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
        primary: PoolMember,
        fallback: PoolMember | None,
        *,
        cooldown: timedelta,
        alerter: Alerter,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if fallback is not None and fallback.provider == primary.provider:
            raise ValueError("the fallback must be a different provider")
        self._members = (primary,) if fallback is None else (primary, fallback)
        self._cooldown = cooldown.total_seconds()
        self._alerter = alerter
        self._monotonic = monotonic
        self._cooling_until: dict[str, float] = {}

    def for_turn(self) -> "PooledModel":
        """The first member not cooling down; the primary (failing fast) when all are."""
        for member in self._members:
            if not self.cooling(member.provider):
                return PooledModel(member, self)
        return PooledModel(self._members[0], self)

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

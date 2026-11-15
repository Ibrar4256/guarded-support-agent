"""Choosing a provider per turn and cooling one down (ADR-023 Part 3)."""

from datetime import timedelta

import pytest
from agent_testing.fakes import RecordingAlerter
from agent_testing.scripted_llm import ScriptedLLM, Turn
from llm_wire import FakeClock

from agent.alerts import Severity
from agent.llm.errors import AuthFailed, InvalidRequest, QuotaExhausted
from agent.llm.pool import COOLDOWN_ALERT, INVALID_REQUEST_PAGE, PoolMember, ProviderPool
from agent.llm.types import ChatModel, Purpose

COOLDOWN = timedelta(seconds=900)
MESSAGES = [{"role": "user", "content": "hi"}]


def member(name: str, *turns: Turn) -> PoolMember:
    return PoolMember(name, f"{name}-model", ScriptedLLM(list(turns), provider=name))


def scripted(pool_member: PoolMember) -> ScriptedLLM:
    assert isinstance(pool_member.model, ScriptedLLM)
    return pool_member.model


async def ask(model: ChatModel) -> str:
    response = await model.complete(MESSAGES, [], deadline=60.0, purpose=Purpose.TURN)
    return response.provider


def pool(
    primary: PoolMember, fallback: PoolMember | None, clock: FakeClock, alerter: RecordingAlerter
) -> ProviderPool:
    return ProviderPool(
        primary, fallback, cooldown=COOLDOWN, alerter=alerter, monotonic=clock.monotonic
    )


async def test_a_spent_quota_cools_the_primary_and_the_next_turn_uses_the_fallback() -> None:
    clock, alerter = FakeClock(), RecordingAlerter()
    primary = member("groq", Turn(raises=QuotaExhausted("daily limit")))
    fallback = member("gemini", Turn(text="from gemini"))
    providers = pool(primary, fallback, clock, alerter)

    first_turn = providers.for_turn()
    with pytest.raises(QuotaExhausted):
        await ask(first_turn)

    assert alerter.kinds(Severity.ALERT) == [COOLDOWN_ALERT]
    assert await ask(providers.for_turn()) == "gemini"


async def test_a_turn_never_switches_provider_mid_loop() -> None:
    clock, alerter = FakeClock(), RecordingAlerter()
    primary = member("groq", Turn(raises=QuotaExhausted("daily limit")))
    fallback = member("gemini", Turn(text="never for this turn", repeat=True))
    turn = pool(primary, fallback, clock, alerter).for_turn()

    with pytest.raises(QuotaExhausted):
        await ask(turn)
    with pytest.raises(QuotaExhausted, match="cooling down"):
        await ask(turn)  # the same turn stays on its provider, failing fast

    assert turn.provider == "groq"
    assert scripted(fallback).calls == []


async def test_with_no_fallback_a_cooling_primary_fails_fast_without_a_call() -> None:
    clock, alerter = FakeClock(), RecordingAlerter()
    primary = member("groq", Turn(raises=AuthFailed("bad key")))
    providers = pool(primary, None, clock, alerter)

    with pytest.raises(AuthFailed):
        await ask(providers.for_turn())
    with pytest.raises(QuotaExhausted, match="cooling down"):
        await ask(providers.for_turn())

    assert len(scripted(primary).calls) == 1


async def test_the_cooldown_ends() -> None:
    clock, alerter = FakeClock(), RecordingAlerter()
    primary = member("groq", Turn(raises=QuotaExhausted("daily")), Turn(text="back"))
    providers = pool(primary, None, clock, alerter)
    with pytest.raises(QuotaExhausted):
        await ask(providers.for_turn())

    clock.now += COOLDOWN.total_seconds()

    assert await ask(providers.for_turn()) == "groq"


async def test_an_invalid_request_pages_and_cools_nothing() -> None:
    clock, alerter = FakeClock(), RecordingAlerter()
    primary = member("groq", Turn(raises=InvalidRequest("unknown parameter")), Turn(text="ok"))
    providers = pool(primary, None, clock, alerter)

    with pytest.raises(InvalidRequest):
        await ask(providers.for_turn())

    assert alerter.kinds(Severity.PAGE) == [INVALID_REQUEST_PAGE]
    assert await ask(providers.for_turn()) == "groq"


def test_the_fallback_must_be_another_provider() -> None:
    with pytest.raises(ValueError, match="different provider"):
        ProviderPool(member("groq"), member("groq"), cooldown=COOLDOWN, alerter=RecordingAlerter())

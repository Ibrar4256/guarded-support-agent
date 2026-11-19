"""Choosing a model per turn from its purpose's ordered list, and cooling a provider down
(ADR-023 Part 3, ADR-025 S1)."""

from collections.abc import Sequence
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


async def ask(model: ChatModel, purpose: Purpose = Purpose.TURN) -> str:
    response = await model.complete(MESSAGES, [], deadline=60.0, purpose=purpose)
    return response.provider


def pool(
    turn: Sequence[PoolMember],
    clock: FakeClock,
    alerter: RecordingAlerter,
    summary: Sequence[PoolMember] = (),
) -> ProviderPool:
    """``summary`` defaults to a model that fails the test if it's ever called."""
    return ProviderPool(
        {Purpose.TURN: turn, Purpose.SUMMARY: summary or [member("cloudflare")]},
        cooldown=COOLDOWN,
        alerter=alerter,
        monotonic=clock.monotonic,
    )


async def test_a_spent_quota_cools_the_primary_and_the_next_turn_uses_the_fallback() -> None:
    clock, alerter = FakeClock(), RecordingAlerter()
    primary = member("groq", Turn(raises=QuotaExhausted("daily limit")))
    fallback = member("gemini", Turn(text="from gemini"))
    providers = pool([primary, fallback], clock, alerter)

    first_turn = providers.model_for(Purpose.TURN)
    with pytest.raises(QuotaExhausted):
        await ask(first_turn)

    assert alerter.kinds(Severity.ALERT) == [COOLDOWN_ALERT]
    assert await ask(providers.model_for(Purpose.TURN)) == "gemini"


async def test_the_first_entry_not_cooling_serves_down_a_longer_list() -> None:
    clock, alerter = FakeClock(), RecordingAlerter()
    first = member("groq", Turn(raises=QuotaExhausted("daily limit")))
    second = member("gemini", Turn(raises=AuthFailed("bad key")))
    third = member("openrouter", Turn(text="from openrouter"))
    providers = pool([first, second, third], clock, alerter)

    for _ in range(2):
        with pytest.raises((QuotaExhausted, AuthFailed)):
            await ask(providers.model_for(Purpose.TURN))

    assert await ask(providers.model_for(Purpose.TURN)) == "openrouter"
    assert alerter.kinds(Severity.ALERT) == [COOLDOWN_ALERT, COOLDOWN_ALERT]


async def test_a_turn_never_switches_provider_mid_loop() -> None:
    clock, alerter = FakeClock(), RecordingAlerter()
    primary = member("groq", Turn(raises=QuotaExhausted("daily limit")))
    fallback = member("gemini", Turn(text="never for this turn", repeat=True))
    turn = pool([primary, fallback], clock, alerter).model_for(Purpose.TURN)

    with pytest.raises(QuotaExhausted):
        await ask(turn)
    with pytest.raises(QuotaExhausted, match="cooling down"):
        await ask(turn)  # the same turn stays on its provider, failing fast

    assert turn.provider == "groq"
    assert scripted(fallback).calls == []


async def test_the_summary_uses_its_own_list_never_the_turns() -> None:
    clock, alerter = FakeClock(), RecordingAlerter()
    turn_model = member("groq", Turn(text="turn only", repeat=True))
    summary_model = member("gemini", Turn(text="a summary"))
    providers = pool([turn_model], clock, alerter, summary=[summary_model])

    assert await ask(providers.model_for(Purpose.SUMMARY), Purpose.SUMMARY) == "gemini"
    assert scripted(turn_model).calls == []


async def test_a_cooldown_is_the_providers_whichever_purpose_hit_it() -> None:
    clock, alerter = FakeClock(), RecordingAlerter()
    shared = member("groq", Turn(raises=QuotaExhausted("daily limit")))
    summary_fallback = member("gemini", Turn(text="a summary"))
    providers = pool([shared], clock, alerter, summary=[shared, summary_fallback])

    with pytest.raises(QuotaExhausted):
        await ask(providers.model_for(Purpose.TURN))

    assert providers.model_for(Purpose.SUMMARY).provider == "gemini"


async def test_with_no_fallback_a_cooling_primary_fails_fast_without_a_call() -> None:
    clock, alerter = FakeClock(), RecordingAlerter()
    primary = member("groq", Turn(raises=AuthFailed("bad key")))
    providers = pool([primary], clock, alerter)

    with pytest.raises(AuthFailed):
        await ask(providers.model_for(Purpose.TURN))
    with pytest.raises(QuotaExhausted, match="cooling down"):
        await ask(providers.model_for(Purpose.TURN))

    assert len(scripted(primary).calls) == 1


async def test_the_cooldown_ends() -> None:
    clock, alerter = FakeClock(), RecordingAlerter()
    primary = member("groq", Turn(raises=QuotaExhausted("daily")), Turn(text="back"))
    providers = pool([primary], clock, alerter)
    with pytest.raises(QuotaExhausted):
        await ask(providers.model_for(Purpose.TURN))

    clock.now += COOLDOWN.total_seconds()

    assert await ask(providers.model_for(Purpose.TURN)) == "groq"


async def test_an_invalid_request_pages_and_cools_nothing() -> None:
    clock, alerter = FakeClock(), RecordingAlerter()
    primary = member("groq", Turn(raises=InvalidRequest("unknown parameter")), Turn(text="ok"))
    providers = pool([primary], clock, alerter)

    with pytest.raises(InvalidRequest):
        await ask(providers.model_for(Purpose.TURN))

    assert alerter.kinds(Severity.PAGE) == [INVALID_REQUEST_PAGE]
    assert await ask(providers.model_for(Purpose.TURN)) == "groq"


@pytest.mark.parametrize(
    ("turn", "message"),
    [
        (["groq", "gemini", "groq"], "lists a provider twice"),
        ([], "no models for turn"),
    ],
)
def test_a_list_must_be_non_empty_with_each_provider_once(turn: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        pool([member(name) for name in turn], FakeClock(), RecordingAlerter())

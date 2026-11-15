"""One call record per model call, success or failure (ADR-023 Part 4), without a database.

The database recorder (append-only grants, unfenced writes) is in
tests/integration/test_llm_calls.py.
"""

import logging
import uuid
from decimal import Decimal

import pytest
from agent_testing.scripted_llm import ScriptedLLM, Turn
from llm_wire import FakeClock, Wire, adapter, chunk, error_body, status, stream_ok, usage_chunk

from agent.llm.errors import ProviderTimeout, RateLimited
from agent.llm.prices import PriceTable
from agent.llm.recording import CallContext, CallRecord, RecordingModel
from agent.llm.types import ChatModel, Purpose, Usage

PRICES = PriceTable.parse(
    {
        "version": "v-test",
        "currency": "USD",
        "models": {
            "groq/test-model": {
                "input": "1",
                "output": "2",
                "source": "https://example.com/pricing",
                "checked": "2026-11-14",
            }
        },
    }
)
CONTEXT = CallContext(conversation_id=uuid.uuid4(), run_id=uuid.uuid4(), turn_id=uuid.uuid4())
MESSAGES = [{"role": "user", "content": "CUSTOMER SECRET TEXT"}]


def recording(
    inner: ChatModel, records: list[CallRecord], provider: str = "groq"
) -> RecordingModel:
    async def record(call: CallRecord) -> None:
        records.append(call)

    return RecordingModel(
        inner,
        provider=provider,
        model="test-model",
        record=record,
        context=CONTEXT,
        prices=PRICES,
    )


async def call(model: RecordingModel) -> None:
    await model.complete(MESSAGES, [], deadline=60.0, purpose=Purpose.TURN)


async def test_a_429_then_success_is_one_record_with_two_attempts() -> None:
    wire = Wire().then(
        status(429, error_body("slow down")),
        stream_ok([chunk(content="Hi."), chunk(finish="stop"), usage_chunk(1_000, 500)]),
    )
    records: list[CallRecord] = []

    await call(recording(adapter(wire, FakeClock()), records))

    (record,) = records
    assert record.attempts == 2 and record.outcome == "ok"
    assert (record.input_tokens, record.output_tokens) == (1_000, 500)
    assert record.cost_usd == Decimal("0.002") and record.price_version == "v-test"
    assert (record.run_id, record.turn_id, record.step) == (CONTEXT.run_id, CONTEXT.turn_id, 1)


async def test_a_failed_call_is_recorded_uncounted_with_its_attempts() -> None:
    wire = Wire().then(*[status(503, error_body("busy"))] * 3)
    records: list[CallRecord] = []

    with pytest.raises(Exception, match="503"):
        await call(recording(adapter(wire, FakeClock()), records))

    (record,) = records
    assert record.outcome == "provider_unavailable" and record.attempts == 3
    assert record.input_tokens is None and record.usage_completeness == "none"
    assert record.cost_usd is None and record.price_version is None


async def test_an_unpriced_model_is_recorded_with_no_cost() -> None:
    scripted = ScriptedLLM([Turn(text="x", usage=Usage(10, 10))])
    records: list[CallRecord] = []

    await call(recording(scripted, records, provider="scripted"))

    assert records[0].cost_usd is None and records[0].input_tokens == 10


async def test_steps_count_every_call_including_failures() -> None:
    scripted = ScriptedLLM([Turn(text="a"), Turn(raises=ProviderTimeout("t")), Turn(text="c")])
    records: list[CallRecord] = []
    model = recording(scripted, records, provider="scripted")

    await call(model)
    with pytest.raises(ProviderTimeout):
        await call(model)
    await call(model)

    assert [(r.step, r.outcome) for r in records] == [
        (1, "ok"),
        (2, "provider_timeout"),
        (3, "ok"),
    ]


async def test_a_failing_recorder_never_fails_the_call(caplog: pytest.LogCaptureFixture) -> None:
    async def broken(_: CallRecord) -> None:
        raise OSError("database is down")

    model = RecordingModel(
        ScriptedLLM([Turn(text="fine")]),
        provider="scripted",
        model="m",
        record=broken,
        context=CONTEXT,
        prices=PRICES,
    )

    response = await model.complete(MESSAGES, [], deadline=60.0, purpose=Purpose.TURN)

    assert response.text == "fine"
    assert any("record lost" in r.getMessage() for r in caplog.records)


async def test_no_key_or_message_text_reaches_a_log_line_or_a_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    wire = Wire().then(
        status(429, error_body("slow down")), stream_ok([chunk(content="ok"), chunk(finish="stop")])
    )
    records: list[CallRecord] = []

    await call(recording(adapter(wire, FakeClock()), records))

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert logged, "DEBUG logging must actually have captured something"
    for secret in ("test-key", "CUSTOMER SECRET TEXT"):
        assert secret not in logged
        assert secret not in repr(records)


async def test_a_rate_limit_after_retries_is_recorded_as_rate_limited() -> None:
    scripted = ScriptedLLM([Turn(raises=RateLimited("429"))])
    records: list[CallRecord] = []

    with pytest.raises(RateLimited):
        await call(recording(scripted, records, provider="scripted"))

    assert records[0].outcome == "rate_limited"

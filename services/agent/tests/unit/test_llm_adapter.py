"""The OpenAI-compatible adapter at the wire level (ADR-023 Parts 2-3, S1-S2, S6).

Requests go through an ``httpx2.MockTransport``; time is a fake clock except where a real
stall must be cut off by ``asyncio.timeout``.
"""

import asyncio
import time

import httpx2
import pytest
from llm_wire import (
    FakeClock,
    Wire,
    adapter,
    call_delta,
    chunk,
    error_body,
    raises,
    status,
    stream_ok,
    stream_then,
    usage_chunk,
)

from agent.llm.adapter import OpenAICompatibleModel
from agent.llm.errors import (
    AuthFailed,
    ContextTooLong,
    InvalidRequest,
    ProviderTimeout,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
)
from agent.llm.types import FinishReason, ModelResponse, Purpose, UsageCompleteness

MESSAGES = [{"role": "user", "content": "hello"}]
DEADLINE = 60.0
OUTER_BOUND_SECONDS = 5
TEXT_OK = [chunk(content="Hi."), chunk(finish="stop"), usage_chunk(10, 2)]


async def complete(
    model: OpenAICompatibleModel, deltas: list[str] | None = None, deadline: float = DEADLINE
) -> ModelResponse:
    on_text = deltas.append if deltas is not None else None
    return await model.complete(
        MESSAGES, [], deadline=deadline, purpose=Purpose.TURN, on_text=on_text
    )


# --- Streaming and assembly ------------------------------------------------------------------


async def test_split_tool_call_deltas_assemble_like_the_unsplit_form() -> None:
    split = Wire().then(
        stream_ok(
            [
                chunk(tool_calls=[call_delta(0, call_id="c1", name="lookup_order")]),
                chunk(tool_calls=[call_delta(1, call_id="c2", name="escalate")]),
                chunk(tool_calls=[call_delta(0, arguments='{"order')]),
                chunk(tool_calls=[call_delta(1, arguments='{"reason_code": "x"}')]),
                chunk(tool_calls=[call_delta(0, arguments='_id": "o1"}')]),
                chunk(finish="tool_calls"),
                usage_chunk(5, 5),
            ]
        )
    )
    whole = Wire().then(
        stream_ok(
            [
                chunk(
                    tool_calls=[
                        call_delta(
                            0, call_id="c1", name="lookup_order", arguments='{"order_id": "o1"}'
                        ),
                        call_delta(
                            1, call_id="c2", name="escalate", arguments='{"reason_code": "x"}'
                        ),
                    ]
                ),
                chunk(finish="tool_calls"),
                usage_chunk(5, 5),
            ]
        )
    )

    split_response = await complete(adapter(split, FakeClock()))
    whole_response = await complete(adapter(whole, FakeClock()))

    assert split_response.tool_calls == whole_response.tool_calls
    assert [c.arguments for c in split_response.tool_calls] == [
        '{"order_id": "o1"}',
        '{"reason_code": "x"}',
    ]


async def test_on_text_gets_the_deltas_in_order_and_they_join_to_the_text() -> None:
    wire = Wire().then(
        stream_ok(
            [chunk(content="a"), chunk(content="b"), chunk(content="c"), chunk(finish="stop")]
        )
    )
    deltas: list[str] = []

    response = await complete(adapter(wire, FakeClock()), deltas)

    assert deltas == ["a", "b", "c"]
    assert "".join(deltas) == response.text


async def test_a_stream_without_usage_is_uncounted_never_zero() -> None:
    wire = Wire().then(stream_ok([chunk(content="Hi."), chunk(finish="stop")]))

    response = await complete(adapter(wire, FakeClock()))

    assert response.usage is None
    assert response.completeness is UsageCompleteness.NONE


@pytest.mark.parametrize(
    ("finish", "expected"),
    [
        ("stop", FinishReason.STOP),
        ("length", FinishReason.LENGTH),
        ("content_filter", FinishReason.CONTENT_FILTER),
    ],
)
async def test_finish_reasons_are_normalized(finish: str, expected: FinishReason) -> None:
    wire = Wire().then(stream_ok([chunk(content="x"), chunk(finish=finish)]))

    assert (await complete(adapter(wire, FakeClock()))).finish_reason is expected


async def test_output_beyond_the_character_cap_is_refused() -> None:
    wire = Wire().then(stream_ok([chunk(content="x" * 50), chunk(finish="stop")]))

    with pytest.raises(ProviderUnavailable, match="exceeded 10 characters"):
        await complete(adapter(wire, FakeClock(), max_output_chars=10, max_attempts=1))


# --- No retry once text was delivered; callback isolation (Part 2) ---------------------------


async def test_a_stream_that_drops_after_two_text_deltas_fails_without_a_retry() -> None:
    wire = Wire().then(
        stream_then(
            [chunk(content="Your refund "), chunk(content="for order A-12")],
            httpx2.ReadError("reset"),
        ),
        stream_ok(TEXT_OK),  # must stay unused
    )
    deltas: list[str] = []

    with pytest.raises(ProviderUnavailable):
        await complete(adapter(wire, FakeClock()), deltas)

    assert deltas == ["Your refund ", "for order A-12"]
    assert len(wire.requests) == 1


async def test_a_stream_that_drops_before_any_text_is_retried() -> None:
    wire = Wire().then(
        stream_then(
            [chunk(tool_calls=[call_delta(0, call_id="c1", name="lookup_order")])],
            httpx2.ReadError("reset"),
        ),
        stream_ok(TEXT_OK),
    )
    deltas: list[str] = []

    response = await complete(adapter(wire, FakeClock()), deltas)

    assert response.text == "Hi." and response.attempts == 2
    assert deltas == ["Hi."]


async def test_on_text_raising_switches_it_off_without_a_retry() -> None:
    wire = Wire().then(stream_ok([chunk(content="a"), chunk(content="b"), chunk(finish="stop")]))
    seen: list[str] = []

    def disconnected(delta: str) -> None:
        seen.append(delta)
        raise ConnectionResetError("client went away")

    model = adapter(wire, FakeClock())
    response = await model.complete(
        MESSAGES, [], deadline=DEADLINE, purpose=Purpose.TURN, on_text=disconnected
    )

    assert response.text == "ab" and response.attempts == 1
    assert seen == ["a"]
    assert len(wire.requests) == 1


async def test_a_stream_closed_without_a_finish_reason_is_a_drop() -> None:
    wire = Wire().then(stream_ok([chunk(content="Hi")]), stream_ok(TEXT_OK))

    response = await complete(adapter(wire, FakeClock()))  # no on_text: nothing was delivered

    assert response.attempts == 2


# --- Retries inside the deadline (Part 3) -----------------------------------------------------


async def test_a_429_then_success_is_one_response_after_two_attempts() -> None:
    clock = FakeClock()
    wire = Wire().then(status(429, error_body("slow down")), stream_ok(TEXT_OK))

    response = await complete(adapter(wire, clock))

    assert response.attempts == 2
    assert response.completeness is UsageCompleteness.FULL
    assert clock.sleeps == [0.5]


async def test_a_retry_after_longer_than_the_time_left_fails_now_without_sleeping() -> None:
    clock = FakeClock()
    wire = Wire().then(
        status(429, error_body("slow down"), {"retry-after": "30"}), stream_ok(TEXT_OK)
    )

    with pytest.raises(RateLimited):
        await complete(adapter(wire, clock), deadline=20.0)

    assert clock.sleeps == []
    assert len(wire.requests) == 1


async def test_a_retry_after_that_fits_is_honored() -> None:
    clock = FakeClock()
    wire = Wire().then(
        status(429, error_body("slow down"), {"retry-after-ms": "1500"}), stream_ok(TEXT_OK)
    )

    await complete(adapter(wire, clock))

    assert clock.sleeps == [1.5]


async def test_no_retry_starts_with_less_than_the_minimum_attempt_time_left() -> None:
    clock = FakeClock()
    wire = Wire().then(status(503, error_body("busy")), stream_ok(TEXT_OK))

    with pytest.raises(ProviderUnavailable):
        await complete(adapter(wire, clock), deadline=5.2)  # 0.5 s backoff + 5 s minimum > 5.2

    assert len(wire.requests) == 1


async def test_a_stalled_stream_is_cut_off_at_the_deadline() -> None:
    wire = Wire().then(stream_then([chunk(content="Hi")], None))
    started = time.monotonic()

    # The outer bound keeps a regression from hanging the suite; it must never be what fires.
    async with asyncio.timeout(OUTER_BOUND_SECONDS):
        with pytest.raises(ProviderTimeout):
            await complete(adapter(wire), deadline=started + 0.3)

    assert time.monotonic() - started < 2.0


async def test_the_sdks_own_retries_are_off() -> None:
    wire = Wire().then(status(500, error_body("boom")))

    with pytest.raises(ProviderUnavailable):
        await complete(adapter(wire, FakeClock(), max_attempts=1))

    assert len(wire.requests) == 1


async def test_a_failed_attempt_after_its_stream_started_makes_the_usage_partial() -> None:
    wire = Wire().then(
        stream_then([chunk(content="Hel")], httpx2.ReadError("reset")), stream_ok(TEXT_OK)
    )

    response = await complete(adapter(wire, FakeClock()))  # no on_text, so the retry is allowed

    assert response.completeness is UsageCompleteness.PARTIAL


# --- Error classification (Part 3, S1) ------------------------------------------------------


async def test_a_spent_quota_is_not_retried() -> None:
    wire = Wire().then(
        status(429, error_body("You exceeded your quota", code="insufficient_quota")),
        stream_ok(TEXT_OK),
    )

    with pytest.raises(QuotaExhausted):
        await complete(adapter(wire, FakeClock()))

    assert len(wire.requests) == 1


async def test_an_unknown_429_is_a_rate_limit_and_retried() -> None:
    wire = Wire().then(status(429, {"weird": "shape"}), stream_ok(TEXT_OK))

    assert (await complete(adapter(wire, FakeClock()))).attempts == 2


@pytest.mark.parametrize(
    ("code", "body", "expected"),
    [
        (400, error_body("too long", code="context_length_exceeded"), ContextTooLong),
        (400, error_body("unknown parameter: foo", code="invalid_request_error"), InvalidRequest),
        (404, error_body("model not found"), InvalidRequest),
        (401, error_body("bad key"), AuthFailed),
        (403, error_body("forbidden"), AuthFailed),
    ],
)
async def test_non_retryable_errors_are_classified_by_cause(
    code: int, body: dict[str, object], expected: type[Exception]
) -> None:
    wire = Wire().then(status(code, body), stream_ok(TEXT_OK))

    with pytest.raises(expected):
        await complete(adapter(wire, FakeClock()))

    assert len(wire.requests) == 1


async def test_a_dropped_connection_is_provider_unavailable() -> None:
    wire = Wire().then(
        *[raises(lambda request: httpx2.ConnectError("refused", request=request))] * 3
    )

    with pytest.raises(ProviderUnavailable):
        await complete(adapter(wire, FakeClock()))


# --- The request -------------------------------------------------------------------------------


async def test_every_request_streams_with_usage_and_the_output_cap() -> None:
    wire = Wire().then(stream_ok(TEXT_OK))

    await complete(adapter(wire, FakeClock(), max_output_tokens=123))

    request = wire.requests[0]
    assert request["stream"] is True
    assert request["stream_options"] == {"include_usage": True}
    assert request["max_tokens"] == 123
    assert "tools" not in request  # no tools offered: the parameter is omitted

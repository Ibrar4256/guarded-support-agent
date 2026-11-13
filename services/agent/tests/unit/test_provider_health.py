"""Provider breakers and what feeds them (ADR-018 S5, S6, S8 and the state machine). Each check
has a negative control."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from agent_testing.env import example_worker_env
from agent_testing.fakes import RecordingAlerter

from agent.alerts import Severity
from agent.core.refund_outcome import (
    LOOKUP_STATUSES,
    SEND_STATUSES,
    Health,
    HttpResult,
    lookup_health,
    send_health,
)
from agent.worker.provider_health import BreakerState, ProviderBreaker, provider_breakers
from agent.worker.settings import WorkerSettings

SPEC = json.loads(
    (Path(__file__).resolve().parents[4] / "contracts" / "refund_api.openapi.json").read_text()
)
OPEN = timedelta(seconds=30)
MAX_OPEN = timedelta(seconds=100)
PAGE_AFTER = timedelta(seconds=600)
S, F, N = Health.SUCCESS, Health.FAILURE, Health.NEUTRAL
TIMINGS = WorkerSettings.from_env(example_worker_env()).timings


def result(status: int | None, body: Any = None, raw: bytes | None = None) -> HttpResult:
    if raw is None:
        raw = b"" if body is None else json.dumps(body).encode()
    return HttpResult(status=status, body=raw)


# --- what counts (S6) -----------------------------------------------------------------------


def test_the_documented_status_sets_are_the_contracts() -> None:
    def documented(path: str, method: str) -> set[int]:
        return {int(code) for code in SPEC["paths"][path][method]["responses"]}

    assert set(SEND_STATUSES) == documented("/refunds", "post")
    assert set(LOOKUP_STATUSES) == documented("/refunds", "get")


SENDS = [
    ("no response", result(None), F),
    ("500", result(500, {"status": "injected_fault"}), F),
    ("503 deadline", result(503, {"status": "deadline_exceeded"}), F),
    ("502 from an ingress, HTML", result(502, raw=b"<html>Bad Gateway</html>"), F),
    ("404 from a misrouted URL", result(404, {"detail": "Not Found"}), F),
    ("418, undocumented", result(418, {"code": "teapot"}), F),
    ("201", result(201, {"id": "re_1"}), S),
    ("201 that isn't JSON", result(201, raw=b"ok"), F),
    ("business 400", result(400, {"code": "refund_exceeds_charge"}), S),
    ("400 that isn't JSON", result(400, raw=b"<html>"), F),
    ("422 key reuse", result(422, {"status": "key_reuse"}), S),
    ("409 in flight", result(409, {"status": "in_flight"}), N),
    ("429 backpressure", result(429), N),
    ("401", result(401, {"status": "unauthorized"}), N),
    ("403", result(403), N),
]
LOOKUPS = [
    ("no response", result(None), F),
    ("503", result(503, {"status": "unavailable"}), F),
    ("404", result(404, {"detail": "Not Found"}), F),
    ("found", result(200, {"refunds": [{"id": "re_1"}]}), S),
    ("not found", result(200, {"refunds": []}), S),
    ("a mismatched row is still an answer", result(200, {"refunds": [{"id": "x"}]}), S),
    ("200 malformed", result(200, {"refund": []}), F),
    ("200 rows not objects", result(200, {"refunds": [1]}), F),
    ("200 HTML", result(200, raw=b"<html>"), F),
    ("400", result(400, {"code": "invalid_request"}), S),
    ("401", result(401, {"status": "unauthorized"}), N),
]


@pytest.mark.parametrize(("answer", "health"), [pytest.param(*r[1:], id=r[0]) for r in SENDS])
def test_send_health(answer: HttpResult, health: Health) -> None:
    assert send_health(answer) is health


@pytest.mark.parametrize(("answer", "health"), [pytest.param(*r[1:], id=r[0]) for r in LOOKUPS])
def test_lookup_health(answer: HttpResult, health: Health) -> None:
    assert lookup_health(answer) is health


# --- the breaker ----------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def state(breaker: ProviderBreaker) -> BreakerState:
    """Read afresh each time (mypy would otherwise narrow the property after one check)."""
    return breaker.state


def make(
    threshold: int = 3, initial: BreakerState = BreakerState.HALF_OPEN
) -> tuple[ProviderBreaker, RecordingAlerter, Clock]:
    alerter, clock = RecordingAlerter(), Clock()
    breaker = ProviderBreaker(
        "write",
        threshold=threshold,
        open_for=OPEN,
        max_open_for=MAX_OPEN,
        page_after=PAGE_AFTER,
        alerter=alerter,
        monotonic=clock,
        initial=initial,
    )
    return breaker, alerter, clock


def test_production_builds_both_breakers_half_open() -> None:
    """S5, through the factory production uses (not a test's explicit argument)."""
    write, read = provider_breakers(TIMINGS, RecordingAlerter())
    assert (write.path, read.path) == ("write", "read")
    assert (state(write), state(read)) == (BreakerState.HALF_OPEN, BreakerState.HALF_OPEN)


def test_it_starts_half_open_so_the_first_request_is_a_probe() -> None:
    breaker, _, _ = make()
    assert state(breaker) is BreakerState.HALF_OPEN and breaker.probing()
    assert not breaker.in_outage(), "a fresh start isn't an outage"
    breaker.take_probe()
    breaker.record(F)
    assert state(breaker) is BreakerState.OPEN, "one failed probe, not a threshold's worth"


def test_negative_control_starting_closed_spends_a_threshold_after_a_restart() -> None:
    breaker, _, _ = make(initial=BreakerState.CLOSED)
    for _ in range(2):
        breaker.record(F)
    assert state(breaker) is BreakerState.CLOSED


def test_half_open_lets_exactly_one_claim_through() -> None:
    breaker, _, _ = make()
    assert breaker.allows_claim()
    breaker.take_probe()
    assert not breaker.allows_claim(), "the slot is taken"
    with pytest.raises(RuntimeError):
        breaker.take_probe()


def test_a_successful_probe_closes_and_resets_the_backoff() -> None:
    breaker, _, clock = make()
    breaker.take_probe()
    breaker.record(F)  # open for 30 s
    clock.now += 30
    breaker.take_probe()
    breaker.record(F)  # open for 60 s
    clock.now += 60
    breaker.take_probe()
    breaker.record(S)
    assert (state(breaker), breaker.in_outage(), breaker.successes) == (
        BreakerState.CLOSED,
        False,
        1,
    )
    assert breaker.next_open_for() == OPEN, "the backoff starts again from the base"


def test_failed_probes_double_the_open_interval_up_to_the_maximum() -> None:
    breaker, alerter, clock = make()
    intervals: list[float] = []
    for _ in range(5):
        breaker.take_probe()
        breaker.record(F)
        opened = [a for a in alerter.raised if a.kind == "provider_write_open"][-1]
        seconds = opened.fields["open_seconds"]
        assert isinstance(seconds, float)
        intervals.append(seconds)
        clock.now += seconds - 1
        assert not breaker.allows_claim(), "still open a second before the interval ends"
        clock.now += 1
    assert intervals == [30, 60, 100, 100, 100]


def test_closed_opens_after_the_threshold_in_a_row_and_a_success_resets_the_count() -> None:
    breaker, alerter, _ = make(initial=BreakerState.CLOSED)
    for answer in (F, F, S, F, F):
        breaker.record(answer)
    assert state(breaker) is BreakerState.CLOSED, "the success in between reset the count"
    breaker.record(F)
    assert state(breaker) is BreakerState.OPEN
    assert alerter.kinds(Severity.ALERT) == ["provider_write_open"], "each opening alerts once"


def test_neutral_answers_neither_count_nor_reset() -> None:
    breaker, _, _ = make(initial=BreakerState.CLOSED)
    for answer in (F, F, N, N, N):
        breaker.record(answer)
    assert state(breaker) is BreakerState.CLOSED
    breaker.record(F)
    assert state(breaker) is BreakerState.OPEN, "the neutral answers didn't reset the two failures"


def test_a_neutral_probe_answer_or_no_request_frees_the_slot() -> None:
    breaker, _, _ = make()
    breaker.take_probe()
    breaker.record(N)
    assert breaker.probing(), "a 409 settled nothing; another claim may probe"
    breaker.take_probe()
    breaker.release_probe()
    assert breaker.probing(), "the probe claim ended without a request"
    assert state(breaker) is BreakerState.HALF_OPEN


def test_a_run_may_attempt_only_while_closed_or_holding_the_probe() -> None:
    breaker, _, clock = make(threshold=1, initial=BreakerState.CLOSED)
    assert breaker.allows_attempt()
    breaker.record(F)
    assert not breaker.allows_attempt(), "open"
    clock.now += 30
    assert state(breaker) is BreakerState.HALF_OPEN
    assert not breaker.allows_attempt(), "half-open, but this run isn't the probe"
    breaker.take_probe()
    assert breaker.allows_attempt(), "the probe holder sends"


def test_an_outage_pages_once_it_outlasts_the_page_threshold() -> None:
    breaker, alerter, clock = make()
    breaker.take_probe()
    breaker.record(F)
    clock.now += PAGE_AFTER.total_seconds() - 1
    breaker.check_page()
    assert alerter.kinds(Severity.PAGE) == [], "no page below the threshold"
    clock.now += 2
    breaker.check_page()
    assert alerter.kinds(Severity.PAGE) == ["provider_write_open"]
    if breaker.probing():
        breaker.take_probe()
    breaker.record(S)
    breaker.check_page()
    assert alerter.kinds(Severity.PAGE) == ["provider_write_open"], "closed: no more pages"


def test_the_next_open_interval_is_what_a_failed_probe_would_set() -> None:
    breaker, _, clock = make()
    assert breaker.next_open_for() == OPEN
    breaker.take_probe()
    breaker.record(F)
    clock.now += 30
    assert breaker.next_open_for() == 2 * OPEN

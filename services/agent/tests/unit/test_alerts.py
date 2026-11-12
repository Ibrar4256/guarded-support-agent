"""The log alerter (ADR-014 Part 6): structured lines, rate-limited by key, never silently."""

import json
import logging
import uuid
from datetime import timedelta
from typing import Any

import pytest

from agent.alerts import LogAlerter

INTERVAL = timedelta(seconds=300)
RUN_A, RUN_B = uuid.uuid4(), uuid.uuid4()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, dict[str, Any]]]:
    return [
        (r.levelno, json.loads(r.getMessage())) for r in caplog.records if r.name == "agent.alerts"
    ]


@pytest.fixture(autouse=True)
def capture(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="agent.alerts")


def test_alerts_and_pages_are_structured_lines_at_their_levels(
    caplog: pytest.LogCaptureFixture,
) -> None:
    alerter = LogAlerter(INTERVAL, monotonic=Clock())
    alerter.alert("provider_config_read_failed", reason="503")
    alerter.page("refund_api_auth_failed", run_id=RUN_A)
    assert lines(caplog) == [
        (
            logging.WARNING,
            {
                "alert": True,
                "kind": "provider_config_read_failed",
                "reason": "503",
                "run_id": None,
                "suppressed": 0,
            },
        ),
        (
            logging.CRITICAL,
            {
                "page": True,
                "kind": "refund_api_auth_failed",
                "run_id": str(RUN_A),
                "suppressed": 0,
            },
        ),
    ]


def test_repeats_inside_the_interval_are_counted_and_reported_on_the_next_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    alerter = LogAlerter(INTERVAL, monotonic=clock)
    for _ in range(4):
        alerter.alert("provider_config_read_failed")
    clock.now += INTERVAL.total_seconds()
    alerter.alert("provider_config_read_failed")
    assert [fields["suppressed"] for _, fields in lines(caplog)] == [0, 3]


def test_negative_control_an_unlimited_alerter_emits_every_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    alerter = LogAlerter(timedelta(microseconds=1), monotonic=clock)
    for _ in range(4):
        alerter.alert("provider_config_read_failed")
        clock.now += 0.001
    assert len(lines(caplog)) == 4


def test_one_runs_page_doesnt_hide_anothers(caplog: pytest.LogCaptureFixture) -> None:
    alerter = LogAlerter(INTERVAL, monotonic=Clock())
    alerter.page("release_streak_limit", run_id=RUN_A)
    alerter.page("release_streak_limit", run_id=RUN_B)
    alerter.page("release_streak_limit", run_id=RUN_A)
    assert [fields["run_id"] for _, fields in lines(caplog)] == [str(RUN_A), str(RUN_B)]


def test_an_alert_doesnt_suppress_a_page_of_the_same_kind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    alerter = LogAlerter(INTERVAL, monotonic=Clock())
    alerter.alert("refund_api_auth_failed")
    alerter.page("refund_api_auth_failed")
    assert [level for level, _ in lines(caplog)] == [logging.WARNING, logging.CRITICAL]


def test_the_interval_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        LogAlerter(timedelta(0))

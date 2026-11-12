"""Alerts and pages (ADR-014 Part 6).

``alert`` asks a person to look soon; ``page`` asks now. For now both are structured log
lines (``WARNING`` and ``CRITICAL``); a real channel plugs in behind the same protocol.

**Rate-limited by key:** ``(severity, kind, run_id)``, so one run's page can't hide another's.
At most one line per key per interval. Repeats inside the interval are counted, not dropped
silently: the next line for that key carries ``suppressed=<n>``.
"""

import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from typing import Protocol

logger = logging.getLogger("agent.alerts")


class Severity(StrEnum):
    ALERT = "alert"
    PAGE = "page"


LEVELS = {Severity.ALERT: logging.WARNING, Severity.PAGE: logging.CRITICAL}


class Alerter(Protocol):
    def alert(self, kind: str, *, run_id: uuid.UUID | None = None, **fields: object) -> None: ...

    def page(self, kind: str, *, run_id: uuid.UUID | None = None, **fields: object) -> None: ...


class LogAlerter:
    def __init__(
        self, interval: timedelta, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        if interval <= timedelta(0):
            raise ValueError("alert interval must be positive")
        self._interval = interval.total_seconds()
        self._monotonic = monotonic
        self._last_emitted: dict[tuple[Severity, str, uuid.UUID | None], float] = {}
        self._suppressed: dict[tuple[Severity, str, uuid.UUID | None], int] = {}

    def alert(self, kind: str, *, run_id: uuid.UUID | None = None, **fields: object) -> None:
        self._emit(Severity.ALERT, kind, run_id, fields)

    def page(self, kind: str, *, run_id: uuid.UUID | None = None, **fields: object) -> None:
        self._emit(Severity.PAGE, kind, run_id, fields)

    def _emit(
        self,
        severity: Severity,
        kind: str,
        run_id: uuid.UUID | None,
        fields: dict[str, object],
    ) -> None:
        key = (severity, kind, run_id)
        now = self._monotonic()
        last = self._last_emitted.get(key)
        if last is not None and now - last < self._interval:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return
        self._last_emitted[key] = now
        record: dict[str, object] = {
            severity.value: True,
            "kind": kind,
            "run_id": str(run_id) if run_id else None,
            "suppressed": self._suppressed.pop(key, 0),
            **fields,
        }
        logger.log(LEVELS[severity], json.dumps(record, default=str, sort_keys=True))

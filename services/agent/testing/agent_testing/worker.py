"""``python -m agent_testing.worker``: the crash-test worker (ADR-004 crash tests, ADR-014
Part 5). The production wiring plus hooks that ``os._exit(1)`` at one named crash point:
no ``finally`` runs and no connection closes cleanly, which is the point of the test.

It refuses to start unless ``DEBUG_FAULT_INJECTION=true``, and never with
``APP_ENV=production``.
"""

import os
import sys
import uuid
from collections.abc import Callable, Mapping

from agent.worker.app import EXIT_CONFIG, main
from agent.worker.hooks import NO_FAULTS, WorkerHooks

CRASH_POINTS = frozenset({"before_refund_call", "after_refund_response"})
CRASH_EXIT_CODE = 1


class FaultConfigError(RuntimeError):
    pass


class CrashHooks(WorkerHooks):
    def __init__(self, point: str, exit_now: Callable[[int], object] = os._exit) -> None:
        self.point = point
        self._exit = exit_now

    async def before_refund_call(self, run_id: uuid.UUID) -> None:
        self._crash_at("before_refund_call")

    async def after_refund_response(self, run_id: uuid.UUID) -> None:
        self._crash_at("after_refund_response")

    def _crash_at(self, point: str) -> None:
        if point == self.point:
            self._exit(CRASH_EXIT_CODE)


def hooks_from_env(environ: Mapping[str, str]) -> WorkerHooks:
    if environ.get("APP_ENV") == "production":
        raise FaultConfigError("the crash-test worker never runs with APP_ENV=production")
    if environ.get("DEBUG_FAULT_INJECTION") != "true":
        raise FaultConfigError("the crash-test worker runs only with DEBUG_FAULT_INJECTION=true")
    point = environ.get("CRASH_POINT", "")
    if not point:
        return NO_FAULTS
    if point not in CRASH_POINTS:
        raise FaultConfigError(f"unknown CRASH_POINT {point!r}; known: {sorted(CRASH_POINTS)}")
    return CrashHooks(point)


def crash_worker_main(environ: Mapping[str, str]) -> int:
    try:
        hooks = hooks_from_env(environ)
    except FaultConfigError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    return main(environ, hooks)


if __name__ == "__main__":
    sys.exit(crash_worker_main(os.environ))

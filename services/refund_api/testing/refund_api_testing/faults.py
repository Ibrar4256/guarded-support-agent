"""Fault plans and the hooks that apply them (ADR-004 failure modes, ADR-010 Part 3).

A plan belongs to one ``action_id``, so tests running in parallel can't affect each other.
Consumption is per mode:
- the crash modes (fail_before_commit, fail_after_commit, timeout_after_commit,
  slow_commit) are one-shot: the first request that reaches their point uses them up;
- dedup_disabled and a retention override stay in effect until the plan is removed,
  because a one-shot dedup_disabled would let the retry deduplicate and the negative
  control could never fail.
There is no TTL: a plan that silently expired would let a test pass in normal mode. Every
applied fault is logged per action, and tests assert the log, so a fault that never fired
fails the test instead.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

from refund_api.hooks import RefundHooks, RequestContext

LOG = logging.getLogger("refund_api_testing.faults")


class FaultMode(StrEnum):
    NORMAL = "normal"
    FAIL_BEFORE_COMMIT = "fail_before_commit"
    FAIL_AFTER_COMMIT = "fail_after_commit"
    TIMEOUT_AFTER_COMMIT = "timeout_after_commit"
    SLOW_COMMIT = "slow_commit"
    DEDUP_DISABLED = "dedup_disabled"


ONE_SHOT = frozenset(
    {
        FaultMode.FAIL_BEFORE_COMMIT,
        FaultMode.FAIL_AFTER_COMMIT,
        FaultMode.TIMEOUT_AFTER_COMMIT,
        FaultMode.SLOW_COMMIT,
    }
)
NEEDS_SECONDS = frozenset({FaultMode.TIMEOUT_AFTER_COMMIT, FaultMode.SLOW_COMMIT})
RETENTION_OVERRIDE = "key_retention_override"


class InjectedFault(Exception):
    """Raised by a fault hook; the test app turns it into a 500."""


@dataclass
class FaultPlan:
    mode: FaultMode
    seconds: float | None = None
    key_retention: timedelta | None = None
    used: bool = False

    def __post_init__(self) -> None:
        if self.mode in NEEDS_SECONDS and (self.seconds is None or self.seconds <= 0):
            raise ValueError(f"{self.mode} needs a positive 'seconds'")
        if self.key_retention is not None and self.key_retention <= timedelta(0):
            raise ValueError("key_retention must be positive")


@dataclass
class FaultBook:
    """Plans and the applied-fault log, per action. One process, one event loop."""

    plans: dict[str, FaultPlan] = field(default_factory=dict)
    applied: dict[str, list[str]] = field(default_factory=dict)

    def register(self, action_id: str, plan: FaultPlan) -> None:
        self.plans[action_id] = plan
        self.applied.setdefault(action_id, [])

    def remove(self, action_id: str) -> None:
        self.plans.pop(action_id, None)
        self.applied.pop(action_id, None)

    def reset(self) -> None:
        self.plans.clear()
        self.applied.clear()

    def active(self, action_id: str, mode: FaultMode) -> FaultPlan | None:
        plan = self.plans.get(action_id)
        if plan is None or plan.mode is not mode or plan.used:
            return None
        return plan

    def record(self, action_id: str, what: str) -> None:
        self.applied.setdefault(action_id, []).append(what)
        LOG.warning("fault applied: action_id=%s fault=%s", action_id, what)

    def fire(self, action_id: str, mode: FaultMode) -> FaultPlan | None:
        """The plan for this action and mode, consumed if one-shot; None if not planned."""
        plan = self.active(action_id, mode)
        if plan is None:
            return None
        if mode in ONE_SHOT:
            plan.used = True
        self.record(action_id, mode)
        return plan


class FaultHooks(RefundHooks):
    def __init__(self, book: FaultBook) -> None:
        self.book = book

    def deduplicates(self, context: RequestContext) -> bool:
        return self.book.fire(context.action_id, FaultMode.DEDUP_DISABLED) is None

    def key_retention(self, context: RequestContext, configured: timedelta) -> timedelta:
        plan = self.book.plans.get(context.action_id)
        if plan is None or plan.key_retention is None:
            return configured
        self.book.record(context.action_id, RETENTION_OVERRIDE)
        return plan.key_retention

    async def before_commit(self, context: RequestContext) -> None:
        if self.book.fire(context.action_id, FaultMode.FAIL_BEFORE_COMMIT):
            raise InjectedFault(FaultMode.FAIL_BEFORE_COMMIT)
        slow = self.book.fire(context.action_id, FaultMode.SLOW_COMMIT)
        if slow and slow.seconds:
            await asyncio.sleep(slow.seconds)

    async def after_commit(self, context: RequestContext) -> None:
        if self.book.fire(context.action_id, FaultMode.FAIL_AFTER_COMMIT):
            raise InjectedFault(FaultMode.FAIL_AFTER_COMMIT)
        hang = self.book.fire(context.action_id, FaultMode.TIMEOUT_AFTER_COMMIT)
        if hang and hang.seconds:
            await asyncio.sleep(hang.seconds)

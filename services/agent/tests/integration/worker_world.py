"""Committed test runs for the worker's integration tests, set up and torn down as the owner.

The worker code runs as the runtime role over an async engine, as in production; the test
arranges and inspects state as the owner (the runtime role can't delete).

These calls are synchronous. Inside an async test, call them with ``asyncio.to_thread`` while
any worker transaction may be open: a sync call blocking on a row lock held by a transaction on
the same event loop would otherwise freeze the loop, so that transaction can never commit (a
hang Postgres can't detect, past any ``asyncio.timeout``). Every owner transaction also sets
``lock_timeout``, so such a mistake fails loudly instead of hanging.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from conftest import T0
from sqlalchemy import Connection, Engine, Row, delete, func, insert, select, text, update

from agent.core.capacity import ApprovalSource
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.db.schema import conversations, customers, policy_controls, runs

WINDOW_MS = 60_000
OWNER_LOCK_TIMEOUT = "2s"


class World:
    def __init__(self, owner: Engine) -> None:
        self.owner = owner
        self.user = f"worker_test_{uuid.uuid4().hex[:8]}"
        with self.transaction() as connection:
            connection.execute(insert(customers).values(id=self.user))

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.owner.begin() as connection:
            connection.execute(text(f"SET LOCAL lock_timeout = '{OWNER_LOCK_TIMEOUT}'"))
            yield connection

    def add_run(
        self,
        status: RunStatus = RunStatus.APPROVED,
        *,
        approved_at: datetime = T0,
        source: ApprovalSource = ApprovalSource.POLICY,
        **extra: Any,
    ) -> uuid.UUID:
        conversation, run = uuid.uuid4(), uuid.uuid4()
        values: dict[str, Any] = {
            "id": run,
            "conversation_id": conversation,
            "user_id": self.user,
            "status": status,
            "refund_outcome": RefundOutcome.NONE,
        }
        if status in {
            RunStatus.APPROVED,
            RunStatus.EXECUTING,
            RunStatus.NEEDS_RECONCILIATION,
            RunStatus.COMPLETED,
        }:
            values.update(approval_source=source, approved_at=approved_at)
            if source is ApprovalSource.REVIEWER:
                values["approved_by"] = "reviewer_bob"
        if status in {RunStatus.EXECUTING, RunStatus.NEEDS_RECONCILIATION, RunStatus.COMPLETED}:
            values.update(first_send_started_at=approved_at, send_window_ms=WINDOW_MS)
            values["refund_outcome"] = (
                RefundOutcome.CONFIRMED if status is RunStatus.COMPLETED else RefundOutcome.UNKNOWN
            )
        if status in {RunStatus.NEEDS_RECONCILIATION, RunStatus.ESCALATED}:
            values["escalated_at"] = approved_at
        if status is RunStatus.AWAIT_APPROVAL:
            values["review_deadline"] = approved_at + timedelta(days=1)
        values.update(extra)
        with self.transaction() as connection:
            connection.execute(insert(conversations).values(id=conversation, user_id=self.user))
            connection.execute(insert(runs).values(**values))
        return run

    def set(self, run: uuid.UUID, **values: Any) -> None:
        with self.transaction() as connection:
            connection.execute(update(runs).where(runs.c.id == run).values(**values))

    def steal(self, run: uuid.UUID, worker: str, lease: timedelta) -> None:
        """Another worker claims the run, in one statement: what ``claim_run`` does to the
        row, without an expire-then-claim window a heartbeat could land in."""
        with self.transaction() as connection:
            connection.execute(
                update(runs)
                .where(runs.c.id == run)
                .values(
                    worker_id=worker,
                    claim_version=runs.c.claim_version + 1,
                    lease_expires_at=func.now() + lease,
                )
            )

    def expire_lease(self, run: uuid.UUID) -> None:
        with self.transaction() as connection:
            connection.execute(
                text(
                    "UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = :r"
                ),
                {"r": run},
            )

    def row(self, run: uuid.UUID) -> Row[Any]:
        with self.owner.connect() as connection:
            return connection.execute(select(runs).where(runs.c.id == run)).one()

    def seconds_until(self, run: uuid.UUID, column: str) -> float:
        """How far ``column`` is from now(), on the database clock."""
        with self.owner.connect() as connection:
            return float(
                connection.execute(
                    text(f"SELECT EXTRACT(EPOCH FROM {column} - now()) FROM runs WHERE id = :r"),
                    {"r": run},
                ).scalar_one()
            )

    def controls(self, *, sends_enabled: bool = True, breaker_tripped: bool = False) -> None:
        with self.transaction() as connection:
            connection.execute(
                update(policy_controls)
                .where(policy_controls.c.id == 1)
                .values(
                    sends_enabled=sends_enabled,
                    breaker_tripped_at=text("now()") if breaker_tripped else None,
                )
            )

    def teardown(self) -> None:
        self.controls()
        with self.transaction() as connection:
            connection.execute(delete(runs).where(runs.c.user_id == self.user))
            connection.execute(delete(conversations).where(conversations.c.user_id == self.user))
            connection.execute(delete(customers).where(customers.c.id == self.user))

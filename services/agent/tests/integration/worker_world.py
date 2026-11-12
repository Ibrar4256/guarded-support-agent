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
from datetime import UTC, datetime, timedelta
from typing import Any

from conftest import T0
from sqlalchemy import Connection, Engine, Row, delete, func, insert, select, text, update

from agent.core.capacity import ApprovalSource
from agent.core.lifecycle import RefundOutcome, RunStatus
from agent.core.policy import FulfillmentStatus
from agent.db.schema import (
    conversations,
    customers,
    decisions,
    events,
    orders,
    policy_controls,
    run_order_lookups,
    runs,
)
from agent.refund_client.payload import canonical_payload

WINDOW_MS = 60_000
BOUND_TIMEOUT_MS = 8_000
BOUND_BUDGET_MS = 5_000
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

    def add_order(
        self,
        *,
        charged_minor: int = 8_000,
        status: FulfillmentStatus = FulfillmentStatus.DELIVERED,
        chargeback_open: bool = False,
    ) -> str:
        """A delivered order of this customer, delivered three days ago (inside the window)."""
        order = f"ord_{uuid.uuid4().hex[:12]}"
        with self.transaction() as connection:
            connection.execute(
                insert(orders).values(
                    id=order,
                    owner_user_id=self.user,
                    charged_minor=charged_minor,
                    currency="USD",
                    status=status,
                    delivered_at=datetime.now(UTC) - timedelta(days=3),
                    chargeback_open=chargeback_open,
                )
            )
        return order

    def add_refund_run(
        self,
        order: str,
        amount_minor: int | None,
        status: RunStatus = RunStatus.APPROVED,
        *,
        looked_up: bool = True,
        **extra: Any,
    ) -> uuid.UUID:
        """A refund proposal on ``order``, approved an hour ago, looked up by its run, with the
        key and canonical payload saved at approval (ADR-004 step 1)."""
        extra.setdefault("approved_at", datetime.now(UTC) - timedelta(hours=1))
        action = f"act_{uuid.uuid4().hex[:12]}"
        if amount_minor is not None:
            extra.setdefault("idempotency_key", f"key_{uuid.uuid4().hex}")
            extra.setdefault(
                "canonical_payload",
                canonical_payload(
                    order_id=order, amount_minor=amount_minor, currency="USD", action_id=action
                ),
            )
        run = self.add_run(
            status,
            action_id=action,
            order_id=order,
            amount_minor=amount_minor,
            currency="USD",
            **extra,
        )
        if looked_up:
            with self.transaction() as connection:
                connection.execute(insert(run_order_lookups).values(run_id=run, order_id=order))
        return run

    def add_executing(
        self,
        amount_minor: int = 2_000,
        *,
        sent_seconds_ago: float = 1.0,
        window_ms: int = 300_000,
        **extra: Any,
    ) -> uuid.UUID:
        """An EXECUTING refund whose first send started ``sent_seconds_ago`` (database clock is
        the same host's clock in compose; tests leave seconds of margin)."""
        first_send = datetime.now(UTC) - timedelta(seconds=sent_seconds_ago)
        if extra.get("send_attempts", 0) > 0:
            # Sends already recorded carry their ADR-016 bounds, as the attempt record writes.
            extra.setdefault("max_client_timeout_ms", BOUND_TIMEOUT_MS)
            extra.setdefault("in_flight_bound_ms", BOUND_TIMEOUT_MS + BOUND_BUDGET_MS)
        return self.add_refund_run(
            self.add_order(),
            amount_minor,
            RunStatus.EXECUTING,
            approved_at=first_send - timedelta(minutes=1),
            first_send_started_at=first_send,
            last_attempt_at=first_send,
            send_window_ms=window_ms,
            **extra,
        )

    def decisions(self, run: uuid.UUID) -> list[Row[Any]]:
        with self.owner.connect() as connection:
            return list(
                connection.execute(
                    select(decisions).where(decisions.c.run_id == run).order_by(decisions.c.id)
                )
            )

    def events(self, run: uuid.UUID) -> list[Row[Any]]:
        with self.owner.connect() as connection:
            return list(
                connection.execute(
                    select(events).where(events.c.run_id == run).order_by(events.c.id)
                )
            )

    def waiting_locks(self) -> int:
        """Lock requests not yet granted, server-wide (ADR-005's race test watches this)."""
        with self.owner.connect() as connection:
            return int(
                connection.execute(
                    text("SELECT count(*) FROM pg_locks WHERE NOT granted")
                ).scalar_one()
            )

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
        mine = select(runs.c.id).where(runs.c.user_id == self.user)
        with self.transaction() as connection:
            connection.execute(
                delete(run_order_lookups).where(run_order_lookups.c.run_id.in_(mine))
            )
            connection.execute(delete(decisions).where(decisions.c.run_id.in_(mine)))
            connection.execute(delete(events).where(events.c.run_id.in_(mine)))
            connection.execute(delete(runs).where(runs.c.user_id == self.user))
            connection.execute(delete(orders).where(orders.c.owner_user_id == self.user))
            connection.execute(delete(conversations).where(conversations.c.user_id == self.user))
            connection.execute(delete(customers).where(customers.c.id == self.user))

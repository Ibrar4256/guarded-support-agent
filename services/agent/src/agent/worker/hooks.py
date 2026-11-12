"""The worker's injection seams (ADR-007, ADR-014 Part 5).

Every method does nothing in production. Tests pass their own hooks to the worker; the
crash entry point in ``agent_testing`` installs ones that ``os._exit``. There's no
``if testing`` branch in production logic.
"""

import uuid


class WorkerHooks:
    async def after_capacity_read(self, run_id: uuid.UUID) -> None:
        """ADR-005's race seam: inside the customer-locked pre-send transaction, right after
        the capacity inputs are read and before the policy check."""

    async def before_refund_call(self, run_id: uuid.UUID) -> None:
        """ADR-004 crash point: the attempt is recorded and the key saved; nothing sent yet."""

    async def after_refund_response(self, run_id: uuid.UUID) -> None:
        """ADR-004 crash point: refund-api answered; the agent hasn't recorded it yet."""


NO_FAULTS = WorkerHooks()

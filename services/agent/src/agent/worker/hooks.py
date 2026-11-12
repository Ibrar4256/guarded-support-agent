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


NO_FAULTS = WorkerHooks()

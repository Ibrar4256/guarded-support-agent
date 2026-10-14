"""Injection seams (ADR-007): production runs these defaults; tests pass a subclass.

There is no ``if testing:`` anywhere in production code. A test that needs to pause a
request inside its transaction, or force two keys onto one lock, overrides a method here.
"""

import hashlib


class RefundHooks:
    def lock_key(self, idempotency_key: str) -> int:
        """The advisory-lock ID for a key: the first 8 bytes of its SHA-256, signed."""
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big", signed=True)

    async def before_transaction(self, idempotency_key: str) -> None:
        """Called after receipt, before a connection is taken. Default: nothing."""

    async def before_commit(self, idempotency_key: str) -> None:
        """Called with the locks held and every row written. Default: nothing."""

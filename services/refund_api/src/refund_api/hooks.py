"""Injection seams (ADR-007, ADR-011): production runs these defaults; tests pass a subclass.

There is no ``if testing:`` anywhere in production code. The fault-injection build
(``refund_api_testing``) and the race tests override methods here: to pause a request
inside its transaction, fail after a commit, force two keys onto one lock, turn off
deduplication for one action, or shorten key retention for one action.
"""

import hashlib
from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class RequestContext:
    """What a hook knows about the request: fault plans are keyed by ``action_id``."""

    idempotency_key: str
    action_id: str


class RefundHooks:
    def lock_key(self, context: RequestContext) -> int:
        """The advisory-lock ID for a key: the first 8 bytes of its SHA-256, signed."""
        digest = hashlib.sha256(context.idempotency_key.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big", signed=True)

    def deduplicates(self, context: RequestContext) -> bool:
        """Whether the idempotency key is honored. Only dedup_disabled says no."""
        return True

    def key_retention(self, context: RequestContext, configured: timedelta) -> timedelta:
        """Retention passed to prune_expired_key; the database floor still applies."""
        return configured

    async def before_transaction(self, context: RequestContext) -> None:
        """After receipt, before a connection is taken. Default: nothing."""

    async def before_commit(self, context: RequestContext) -> None:
        """With the locks held and every row written. Default: nothing."""

    async def after_commit(self, context: RequestContext) -> None:
        """After a new refund committed, before the response is sent. Default: nothing."""

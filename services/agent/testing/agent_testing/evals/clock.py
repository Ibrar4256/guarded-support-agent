"""Eval-time sources of time, ids and fence nonces (ADR-026 Part 2, S3).

Each is a pure function of its seed and of how many times it has been read, so a case built
twice, or recorded live and then replayed, writes the same stamps and ids and sends the same
prompts. ``ANCHOR`` is a fixed past instant at midday: changing it changes every date a prompt
shows, and with them every recording.
"""

import random
import uuid
from datetime import UTC, datetime, timedelta

ANCHOR = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
TICK = timedelta(seconds=1)


class LogicalClock:
    """``ANCHOR + n * TICK``, ``n`` counting readings: ordered, never the real time."""

    def __init__(self, anchor: datetime = ANCHOR, tick: timedelta = TICK) -> None:
        self._anchor = anchor
        self._tick = tick
        self.readings = 0

    def stamp(self) -> datetime:
        self.readings += 1
        return self._anchor + self.readings * self._tick


class SeededIds:
    """Version-4-shaped UUIDs from a seeded generator."""

    def __init__(self, seed: str) -> None:
        self._random = random.Random(f"ids:{seed}")

    def __call__(self) -> uuid.UUID:
        return uuid.UUID(int=self._random.getrandbits(128), version=4)


class SeededNonces:
    """Fence nonces shaped like ``new_nonce()``'s (16 hex characters)."""

    def __init__(self, seed: str) -> None:
        self._random = random.Random(f"nonces:{seed}")

    def __call__(self) -> str:
        return f"{self._random.getrandbits(64):016x}"

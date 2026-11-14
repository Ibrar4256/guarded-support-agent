"""The minimal tool contract (ADR-020 Part 1); the loop (ADR-001, ADR-006) consumes it.

A tool is an arguments model, which is exactly what the model fills in and doubles as the
provider schema, plus an async function taking a ``ToolContext`` built by code. Identity
(the session user) lives only in the context, never in the arguments. A tool returns the
model-facing text: one canonical JSON string, so "byte for byte" means what it says.
"""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection


class ToolArgs(BaseModel):
    """Strict: no extra fields, no type coercion."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


@dataclass(frozen=True)
class ToolContext:
    """Built by the loop from the authenticated session and the turn it holds.

    ``conn`` must not be inside a transaction: tools open their own.
    """

    session_user: str
    conversation_id: uuid.UUID
    turn_id: uuid.UUID
    run_id: uuid.UUID
    conn: AsyncConnection


class LostTurn(Exception):
    """This turn no longer holds its conversation's lease (ADR-008); the loop stops the turn."""


def canonical(result: Mapping[str, object]) -> str:
    return json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)

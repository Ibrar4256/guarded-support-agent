"""Request and response models: the shape of the HTTP contract (ADR-004, ADR-010).

Each response has exactly one meaning to the agent. Only 400 carries a business-error
``code``; 409, 422 and 503 have their own schemas with a ``status`` and no ``code``, so none
of them can be mistaken for a rejection.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Interim formats (ADR-010); the final action_id format follows the agent's ID generator.
ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
KEY_PATTERN = r"^[A-Za-z0-9_-]{1,255}$"
CURRENCY_PATTERN = r"^[A-Z]{3}$"
BIGINT_MAX = 2**63 - 1


class BusinessErrorCode(StrEnum):
    # Treated by the agent as unknown, not rejected (ADR-010 Amends 1).
    INVALID_REQUEST = "invalid_request"
    REFUND_EXCEEDS_CHARGE = "refund_exceeds_charge"
    CHARGE_NOT_FOUND = "charge_not_found"
    CURRENCY_MISMATCH = "currency_mismatch"


class RefundRequest(BaseModel):
    # strict: "2000", 20.0 and true are rejected for amount_minor, never coerced.
    model_config = ConfigDict(extra="forbid", strict=True)

    order_id: str = Field(pattern=ID_PATTERN)
    amount_minor: int = Field(ge=1, le=BIGINT_MAX)
    currency: str = Field(pattern=CURRENCY_PATTERN)
    action_id: str = Field(
        pattern=ID_PATTERN,
        description=(
            "ADR-004 step 2's `metadata.action_id`: the agent action that caused this refund, "
            "used by the reconcile lookup. Flat by design, see ADR-010 formats; a provider "
            "adapter maps it into the provider's own metadata."
        ),
    )


class Refund(BaseModel):
    id: UUID
    order_id: str
    action_id: str
    amount_minor: int
    currency: str
    created_at: datetime


class RefundList(BaseModel):
    refunds: list[Refund]


class BusinessError(BaseModel):
    code: BusinessErrorCode
    # Locations only (e.g. "body.amount_minor"), never values (ADR-010 Part 1).
    fields: list[str] = Field(default_factory=list)


class InFlight(BaseModel):
    status: Literal["in_flight"] = "in_flight"


class KeyMismatch(BaseModel):
    status: Literal["key_payload_mismatch"] = "key_payload_mismatch"


class Unavailable(BaseModel):
    status: Literal["deadline_exceeded", "unavailable"]


class Unauthorized(BaseModel):
    status: Literal["unauthorized"] = "unauthorized"

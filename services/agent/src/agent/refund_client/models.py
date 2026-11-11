"""The agent's own request model for refund-api (ADR-007: duplicated on purpose, kept honest
by the contract test against ``contracts/refund_api.openapi.json``)."""

from pydantic import BaseModel, ConfigDict, Field

from agent.core.refund_outcome import SentRefund

ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
KEY_PATTERN = r"^[A-Za-z0-9_-]{1,255}$"
CURRENCY_PATTERN = r"^[A-Z]{3}$"
BIGINT_MAX = 2**63 - 1


class RefundPayload(BaseModel):
    """What the agent sends. Built from the canonical payload saved at approval (ADR-004
    step 1), never re-derived at send time."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    order_id: str = Field(pattern=ID_PATTERN)
    amount_minor: int = Field(ge=1, le=BIGINT_MAX)
    currency: str = Field(pattern=CURRENCY_PATTERN)
    action_id: str = Field(pattern=ID_PATTERN)

    def as_sent(self) -> SentRefund:
        return SentRefund(
            order_id=self.order_id,
            amount_minor=self.amount_minor,
            currency=self.currency,
            action_id=self.action_id,
        )

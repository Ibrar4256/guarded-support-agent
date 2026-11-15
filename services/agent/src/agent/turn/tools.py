"""The tools a turn offers the model (ADR-020 contract, ADR-022 S1/S7).

Each tool's schema is derived from its arguments model, so identity can't be in it. Read tools
run inside the loop; a write tool never runs there: a valid write call, alone in its response,
ends the loop as a proposal that code evaluates (ADR-001).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from agent.tools.contract import ToolArgs, ToolContext
from agent.tools.order_lookup import OrderLookupArgs, lookup_order


def tool_schema(name: str, description: str, args: type[ToolArgs]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": args.model_json_schema(),
        },
    }


@dataclass(frozen=True)
class ReadTool:
    name: str
    description: str
    args: type[ToolArgs]
    run: Callable[[ToolContext, Any], Awaitable[str]]

    def schema(self) -> dict[str, Any]:
        return tool_schema(self.name, self.description, self.args)


@dataclass(frozen=True)
class WriteTool:
    name: str
    description: str
    args: type[ToolArgs]

    def schema(self) -> dict[str, Any]:
        return tool_schema(self.name, self.description, self.args)


class ProposeRefundArgs(ToolArgs):
    """S7: no identity field; ``amount_minor`` is the unit lookup_order calls ``charged_minor``."""

    order_id: str = Field(description="The order to refund, as lookup_order returned it.")
    amount_minor: int = Field(
        description=(
            "The refund amount in the order's minor units (cents for USD), the same unit "
            "lookup_order returns as charged_minor."
        )
    )
    currency: str = Field(
        pattern="^[A-Z]{3}$", description="The order's currency code, as lookup_order returned it."
    )


EscalationReason = Literal["customer_requested_person", "outside_scope", "other"]


class EscalateArgs(ToolArgs):
    reason_code: EscalationReason = Field(
        description=(
            "customer_requested_person: the customer asked for a person; outside_scope: the "
            "request is something you can't help with; other: anything else."
        )
    )


READ_TOOLS: dict[str, ReadTool] = {
    tool.name: tool
    for tool in (
        ReadTool(
            name="lookup_order",
            description=(
                "Look up one of this customer's orders by its ID. Returns the order's status, "
                "amount and delivery date, or not_found."
            ),
            args=OrderLookupArgs,
            run=lookup_order,
        ),
    )
}

PROPOSE_REFUND = WriteTool(
    name="propose_refund",
    description=(
        "Propose a refund on an order you looked up in this conversation. Policy decides "
        "whether it's approved, sent for review or passed to a person; you don't. Call it "
        "alone, with no other tool call in the same response."
    ),
    args=ProposeRefundArgs,
)
ESCALATE = WriteTool(
    name="escalate",
    description=(
        "Hand the conversation to a person. Call it alone, with no other tool call in the "
        "same response."
    ),
    args=EscalateArgs,
)
WRITE_TOOLS: dict[str, WriteTool] = {tool.name: tool for tool in (PROPOSE_REFUND, ESCALATE)}

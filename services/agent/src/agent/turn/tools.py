"""The tools a turn offers the model (ADR-020 contract, ADR-022 S1/S7).

Each tool's schema is derived from its arguments model, so identity can't be in it. Read tools
run inside the loop; write tools (T4) leave it as a proposal.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agent.tools.contract import ToolArgs, ToolContext
from agent.tools.order_lookup import OrderLookupArgs, lookup_order


@dataclass(frozen=True)
class ReadTool:
    name: str
    description: str
    args: type[ToolArgs]
    run: Callable[[ToolContext, Any], Awaitable[str]]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args.model_json_schema(),
            },
        }


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

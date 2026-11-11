"""Placeholder: the order-lookup tool must answer "not found" identically for a missing
order and another user's order (PROJECT_BRIEF requirement 2, EVAL_PLAN category 4, AS-05).

Strict xfail until ``agent.tools.order_lookup`` exists. The moment the module is added,
this test stops raising ModuleNotFoundError and fails CI instead, until it's replaced with
the real test: compare the two responses byte for byte (same message, shape and code path),
with a planted "belongs to another customer" message as the negative control. If the tool
lands under a different module path, update the import below in the same change.
"""

import importlib

import pytest


@pytest.mark.xfail(
    strict=True,
    raises=ModuleNotFoundError,
    reason="agent.tools.order_lookup not written yet; replace this placeholder when it is",
)
def test_lookup_answers_missing_and_foreign_orders_identically() -> None:
    importlib.import_module("agent.tools.order_lookup")
    pytest.fail(
        "agent.tools.order_lookup exists: write the byte-for-byte uniform not-found test "
        "(with its negative control) and delete this placeholder"
    )

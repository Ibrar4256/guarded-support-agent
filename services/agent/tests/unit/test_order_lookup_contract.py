"""The order-lookup tool's contract without a database (ADR-020 Part 1, S1, S3).

The arguments model is the schema the model sees, so identity can't be in it; the
byte-for-byte uniform not-found test runs against Postgres in
tests/integration/test_order_lookup.py.
"""

import json

import pytest
from pydantic import ValidationError

from agent.tools.contract import canonical
from agent.tools.order_lookup import NOT_FOUND, UNAVAILABLE, OrderLookupArgs

IDENTITY_WORDS = ("user", "customer", "owner", "session", "account")


def test_the_arguments_schema_has_order_id_only() -> None:
    schema = OrderLookupArgs.model_json_schema()

    assert set(schema["properties"]) == {"order_id"}
    assert schema["required"] == ["order_id"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["order_id"]["type"] == "string"


def test_no_property_names_an_identity() -> None:
    names = " ".join(OrderLookupArgs.model_json_schema()["properties"]).lower()

    assert not [word for word in IDENTITY_WORDS if word in names]


@pytest.mark.parametrize(
    "arguments",
    [
        {"order_id": "ord_demo_1001", "user_id": "user_bob"},
        {"order_id": 1001},
        {"order_id": None},
        {},
    ],
)
def test_extra_missing_or_non_string_arguments_are_rejected(arguments: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        OrderLookupArgs.model_validate(arguments)


def test_results_are_canonical_json() -> None:
    assert canonical({"b": 1, "a": [1, 2], "c": "x"}) == '{"a":[1,2],"b":1,"c":"x"}'
    with pytest.raises(ValueError):
        canonical({"a": float("nan")})


@pytest.mark.parametrize("constant", [NOT_FOUND, UNAVAILABLE])
def test_the_fixed_results_are_canonical(constant: str) -> None:
    assert canonical(json.loads(constant)) == constant


def test_not_found_and_unavailable_are_distinct_and_say_nothing_about_ownership() -> None:
    assert NOT_FOUND != UNAVAILABLE
    for text in (NOT_FOUND, UNAVAILABLE):
        lowered = text.lower()
        assert "another" not in lowered
        assert "belongs" not in lowered
        assert "exists" not in lowered

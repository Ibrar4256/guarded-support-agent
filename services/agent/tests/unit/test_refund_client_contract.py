"""The agent's refund client against refund-api's committed spec (ADR-007, ADR-012).

Validated with a standard JSON Schema validator against contracts/refund_api.openapi.json:
- anything the agent's request model accepts, the spec accepts;
- every response the spec declares has an example here that validates against the spec and
  classifies to the outcome ADR-012's table says;
- the business-error enum is identical in both directions.
"""

import copy
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from agent.core.refund_outcome import (
    ConfigReadKind,
    HttpResult,
    LookupKind,
    RefundErrorCode,
    SendKind,
    classify_lookup,
    classify_send,
    read_provider_config,
)
from agent.refund_client.models import BIGINT_MAX, KEY_PATTERN, RefundPayload

SPEC_FILE = Path(__file__).resolve().parents[4] / "contracts" / "refund_api.openapi.json"
SPEC: dict[str, Any] = json.loads(SPEC_FILE.read_text(encoding="utf-8"))
SCHEMAS = SPEC["components"]["schemas"]


def validator(schema: dict[str, Any], spec: dict[str, Any] = SPEC) -> Draft202012Validator:
    """The whole spec as the root, so #/components/schemas/... references resolve. A
    FormatChecker checks ``uuid``; ``date-time`` isn't checked without an extra package, so
    the examples' timestamps are parsed separately below."""
    return Draft202012Validator({**spec, **schema}, format_checker=FormatChecker())


REQUEST = validator({"$ref": "#/components/schemas/RefundRequest"})
VALID = {"order_id": "ord_1", "amount_minor": 1000, "currency": "USD", "action_id": "act_1"}

# --- requests -------------------------------------------------------------------------------

CANDIDATES = {
    "valid": VALID,
    "smallest-amount": VALID | {"amount_minor": 1},
    "largest-amount": VALID | {"amount_minor": BIGINT_MAX},
    "longest-ids": VALID | {"order_id": "o" * 128, "action_id": "a" * 128},
    "id-with-dash-and-underscore": VALID | {"order_id": "ord_1-2"},
    "zero-amount": VALID | {"amount_minor": 0},
    "above-bigint": VALID | {"amount_minor": BIGINT_MAX + 1},
    "string-amount": VALID | {"amount_minor": "1000"},
    "bool-amount": VALID | {"amount_minor": True},
    "lowercase-currency": VALID | {"currency": "usd"},
    "extra-field": VALID | {"metadata": {"action_id": "act_1"}},
    "missing-field": {k: v for k, v in VALID.items() if k != "action_id"},
    "id-too-long": VALID | {"order_id": "o" * 129},
    "id-with-space": VALID | {"order_id": "ord 1"},
}
SPEC_MUST_REJECT = [
    "zero-amount",
    "above-bigint",
    "string-amount",
    "bool-amount",
    "lowercase-currency",
    "extra-field",
    "missing-field",
    "id-too-long",
    "id-with-space",
]


def agent_accepts(payload: dict[str, Any]) -> bool:
    try:
        RefundPayload.model_validate(payload)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize("name", sorted(CANDIDATES))
def test_anything_the_agent_accepts_the_spec_accepts(name: str) -> None:
    payload = CANDIDATES[name]
    if agent_accepts(payload):
        assert REQUEST.is_valid(RefundPayload.model_validate(payload).model_dump())


@pytest.mark.parametrize("name", SPEC_MUST_REJECT)
def test_control_the_spec_validator_really_enforces(name: str) -> None:
    # Without this, a validator that accepts everything would make the test above vacuous.
    assert not REQUEST.is_valid(CANDIDATES[name])
    assert not agent_accepts(CANDIDATES[name])


def test_the_valid_cases_are_accepted_by_both() -> None:
    for name in ("valid", "smallest-amount", "largest-amount", "longest-ids"):
        assert agent_accepts(CANDIDATES[name]) and REQUEST.is_valid(CANDIDATES[name]), name


def test_control_a_drifted_agent_field_would_fail() -> None:
    # A copy of the agent's body with a renamed field, as if the agent's model drifted.
    drifted = {"order": VALID["order_id"]} | {k: v for k, v in VALID.items() if k != "order_id"}
    assert not REQUEST.is_valid(drifted)


# --- responses ------------------------------------------------------------------------------

REFUND: dict[str, Any] = {
    "id": "8f1c2a8e-7c1b-4c5e-9a55-2b6f0c1d2e3f",
    "order_id": "ord_1",
    "amount_minor": 1000,
    "currency": "USD",
    "action_id": "act_1",
    "created_at": "2026-11-12T10:00:00Z",
}
SENT = RefundPayload.model_validate(VALID).as_sent()

# (path, method, status) → (example body, expected outcome)
EXAMPLES: dict[tuple[str, str, str], tuple[Any, str]] = {
    ("/refunds", "post", "201"): (REFUND, SendKind.CONFIRMED),
    ("/refunds", "post", "400"): (
        {"code": "refund_exceeds_charge", "fields": []},
        SendKind.REJECTED,
    ),
    ("/refunds", "post", "401"): ({"status": "unauthorized"}, SendKind.RECONCILE),
    ("/refunds", "post", "409"): ({"status": "in_flight"}, SendKind.RETRY),
    ("/refunds", "post", "422"): ({"status": "key_payload_mismatch"}, SendKind.RECONCILE),
    ("/refunds", "post", "503"): ({"status": "deadline_exceeded"}, SendKind.RETRY),
    ("/refunds", "get", "200"): ({"refunds": [REFUND]}, LookupKind.FOUND),
    ("/refunds", "get", "400"): (
        {"code": "invalid_request", "fields": ["query.order_id"]},
        LookupKind.LOOKUP_FAILED,
    ),
    ("/refunds", "get", "401"): ({"status": "unauthorized"}, LookupKind.LOOKUP_FAILED),
    ("/refunds", "get", "503"): ({"status": "unavailable"}, LookupKind.LOOKUP_FAILED),
    ("/config", "get", "200"): (
        {
            "max_retry_window_ms": 600_000,
            "key_retention_ms": 86_400_000,
            "processing_budget_ms": 5_000,
        },
        ConfigReadKind.OK,
    ),
    ("/config", "get", "401"): ({"status": "unauthorized"}, ConfigReadKind.AUTH_FAILED),
}


def declared(spec: dict[str, Any] = SPEC) -> set[tuple[str, str, str]]:
    return {
        (path, method, status)
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        for status in operation["responses"]
    }


def response_schema(path: str, method: str, status: str) -> dict[str, Any]:
    content = SPEC["paths"][path][method]["responses"][status]["content"]
    schema: dict[str, Any] = content["application/json"]["schema"]
    return schema


def test_every_declared_response_has_an_example_and_no_extra() -> None:
    assert declared() == set(EXAMPLES)


@pytest.mark.parametrize("where", sorted(EXAMPLES), ids=lambda w: f"{w[1]}-{w[2]}")
def test_example_matches_the_spec_and_classifies(where: tuple[str, str, str]) -> None:
    example, expected = EXAMPLES[where]
    validator(response_schema(*where)).validate(example)
    result = HttpResult(status=int(where[2]), body=json.dumps(example).encode())
    if where[0] == "/config":
        read = read_provider_config(result, timedelta(seconds=8), timedelta(minutes=15))
        assert read.kind is expected
    elif where[1] == "post":
        assert classify_send(result, SENT).kind is expected
    else:
        assert classify_lookup(result, SENT).kind is expected


def test_example_timestamps_are_real_date_times() -> None:
    # The FormatChecker can't check date-time without an extra package; parse it instead.
    assert datetime.fromisoformat(REFUND["created_at"]).tzinfo is not None


def test_control_the_format_checker_rejects_a_bad_uuid() -> None:
    refund = validator({"$ref": "#/components/schemas/Refund"})
    assert not refund.is_valid(REFUND | {"id": "not-a-uuid"})


# --- headers and authentication -----------------------------------------------------------


def idempotency_header_schema() -> dict[str, Any]:
    parameters = SPEC["paths"]["/refunds"]["post"]["parameters"]
    header = next(p for p in parameters if p["in"] == "header" and p["name"] == "Idempotency-Key")
    schema: dict[str, Any] = header["schema"]
    return schema


def test_the_agents_keys_match_the_specs_header_schema() -> None:
    # Keys are uuid4 hex strings, generated at approval (ADR-004 step 1).
    header = validator(idempotency_header_schema())
    for _ in range(20):
        assert header.is_valid(uuid.uuid4().hex)
    assert idempotency_header_schema()["pattern"] == KEY_PATTERN


def test_control_the_header_schema_rejects_a_bad_key() -> None:
    header = validator(idempotency_header_schema())
    assert not header.is_valid("key with spaces")
    assert not header.is_valid("k" * 256)


def test_the_spec_declares_bearer_auth() -> None:
    assert SPEC["components"]["securitySchemes"]["bearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert SPEC["security"] == [{"bearerAuth": []}]


# --- the business-error enum, both ways -------------------------------------------------------


def enum_drift(spec: dict[str, Any]) -> set[str]:
    in_spec = set(spec["components"]["schemas"]["BusinessErrorCode"]["enum"])
    return in_spec ^ {code.value for code in RefundErrorCode}


def test_error_codes_are_identical_both_ways() -> None:
    assert enum_drift(SPEC) == set()


def test_control_a_new_server_code_is_caught() -> None:
    changed = copy.deepcopy(SPEC)
    changed["components"]["schemas"]["BusinessErrorCode"]["enum"].append("card_expired")
    assert enum_drift(changed) == {"card_expired"}


@pytest.mark.parametrize("code", sorted(SCHEMAS["BusinessErrorCode"]["enum"]))
def test_every_spec_code_has_a_deliberate_outcome(code: str) -> None:
    result = HttpResult(status=400, body=json.dumps({"code": code, "fields": []}).encode())
    outcome = classify_send(result, SENT)
    expected = SendKind.RECONCILE if code == "invalid_request" else SendKind.REJECTED
    assert outcome.kind is expected
    assert outcome.error_code == RefundErrorCode(code)

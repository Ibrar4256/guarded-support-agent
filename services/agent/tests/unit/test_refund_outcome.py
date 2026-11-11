"""Refund API responses → outcomes (ADR-004 step 3, ADR-008, ADR-010, ADR-012).

Table tests pin every row of ADR-012's table. Properties check the rules over arbitrary
responses, and each property has a negative control: Hypothesis must find a counterexample
in a deliberately broken classifier, or the property proves nothing.
"""

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from hypothesis import HealthCheck, find, given, settings
from hypothesis import strategies as st
from hypothesis.errors import NoSuchExample

from agent.core.refund_outcome import (
    REJECTING_CODES,
    RESPONSE_SIZE_CAP_BYTES,
    ConfigReadKind,
    HttpResult,
    LookupKind,
    RefundErrorCode,
    RetryPolicy,
    SendKind,
    SendOutcome,
    SentRefund,
    classify_lookup,
    classify_send,
    read_provider_config,
    send_allowed,
)

SENT = SentRefund(order_id="ord_1", amount_minor=1_000, currency="USD", action_id="act_1")
REFUND = {
    "id": "8f1c2a8e-7c1b-4c5e-9a55-2b6f0c1d2e3f",
    "order_id": "ord_1",
    "amount_minor": 1_000,
    "currency": "USD",
    "action_id": "act_1",
    "created_at": "2026-11-12T10:00:00Z",
}


def body(value: Any) -> bytes:
    return json.dumps(value).encode()


def send(status: int | None, payload: Any = None, retry_after: str | None = None) -> SendOutcome:
    raw = b"" if payload is None else body(payload)
    return classify_send(HttpResult(status=status, body=raw, retry_after=retry_after), SENT)


# --- every row of the table ---------------------------------------------------------------

TABLE: list[tuple[str, int | None, Any, SendKind, bool]] = [
    ("201 matching", 201, REFUND, SendKind.CONFIRMED, False),
    ("201 other amount", 201, REFUND | {"amount_minor": 999}, SendKind.RECONCILE, True),
    ("201 other action", 201, REFUND | {"action_id": "act_2"}, SendKind.RECONCILE, True),
    ("201 bool amount", 201, REFUND | {"amount_minor": True}, SendKind.RECONCILE, True),
    ("201 not json", 201, None, SendKind.RECONCILE, True),
    ("200 on create", 200, REFUND, SendKind.RECONCILE, True),
    ("no response", None, None, SendKind.RETRY, False),
    ("500", 500, None, SendKind.RETRY, False),
    ("503 deadline", 503, {"status": "deadline_exceeded"}, SendKind.RETRY, False),
    ("408", 408, None, SendKind.RETRY, False),
    ("409 in flight", 409, {"status": "in_flight"}, SendKind.RETRY, False),
    ("429", 429, None, SendKind.RETRY, False),
    *[
        (f"400 {code}", 400, {"code": code.value, "fields": []}, SendKind.REJECTED, False)
        for code in sorted(REJECTING_CODES)
    ],
    (
        "400 invalid_request",
        400,
        {"code": "invalid_request", "fields": []},
        SendKind.RECONCILE,
        True,
    ),
    ("400 unknown code", 400, {"code": "refund_too_big"}, SendKind.RECONCILE, True),
    ("400 no code", 400, {"detail": "x"}, SendKind.RECONCILE, True),
    ("400 code is a list", 400, {"code": ["charge_not_found"]}, SendKind.RECONCILE, True),
    ("422 mismatch", 422, {"status": "key_payload_mismatch"}, SendKind.RECONCILE, True),
    ("401", 401, {"status": "unauthorized"}, SendKind.RECONCILE, True),
    ("403", 403, None, SendKind.RECONCILE, True),
    ("404", 404, None, SendKind.RECONCILE, True),
    ("302", 302, None, SendKind.RECONCILE, True),
]


@pytest.mark.parametrize(
    ("status", "payload", "kind", "alert"),
    [pytest.param(*row[1:], id=row[0]) for row in TABLE],
)
def test_classification_table(
    status: int | None, payload: Any, kind: SendKind, alert: bool
) -> None:
    outcome = send(status, payload)
    assert (outcome.kind, outcome.alert) == (kind, alert)


def test_confirmed_carries_the_refund_id() -> None:
    assert send(201, REFUND).refund_id == REFUND["id"]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("7", timedelta(seconds=7)),
        (" 12 ", timedelta(seconds=12)),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("-3", None),
        ("١٢", None),
        ("9" * 40, timedelta(seconds=999_999_999)),
    ],
    ids=["seconds", "padded", "http-date", "negative", "non-ascii-digits", "absurd"],
)
def test_retry_after_parsing(header: str, expected: timedelta | None) -> None:
    assert send(429, None, retry_after=header).retry_after == expected


def test_retry_after_only_counts_on_429() -> None:
    assert send(503, None, retry_after="5").retry_after is None


# --- properties over arbitrary responses ---------------------------------------------------

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=20),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner, max_size=4)
    ),
    max_leaves=10,
)
bodies = st.one_of(
    st.binary(max_size=40),
    json_values.map(body),
    st.fixed_dictionaries({"code": st.sampled_from([*RefundErrorCode, "nope"])}).map(body),
    st.just(body(REFUND)),
    st.builds(lambda amount: body(REFUND | {"amount_minor": amount}), st.integers()),
)
# Biased toward the statuses the protocol names, so REJECTED and CONFIRMED actually occur;
# uniform 100-599 almost never draws "400 + a business code" (a vacuous property, AS-28).
NAMED_STATUSES = [200, 201, 302, 400, 401, 403, 404, 408, 409, 422, 429, 500, 503]
results = st.builds(
    HttpResult,
    status=st.none() | st.sampled_from(NAMED_STATUSES) | st.integers(min_value=100, max_value=599),
    body=bodies,
    retry_after=st.none() | st.text(max_size=12),
)

Classifier = Callable[[HttpResult, SentRefund], SendOutcome]


def rejects_only_business_codes(classify: Classifier, result: HttpResult) -> bool:
    outcome = classify(result, SENT)
    if outcome.kind is not SendKind.REJECTED:
        return True
    try:
        code = json.loads(result.body)["code"]
    except (ValueError, TypeError, KeyError):
        return False
    return result.status == 400 and code in {c.value for c in REJECTING_CODES}


def confirms_only_matching_201(classify: Classifier, result: HttpResult) -> bool:
    if classify(result, SENT).kind is not SendKind.CONFIRMED:
        return True
    if result.status != 201:
        return False
    try:
        data = json.loads(result.body)
    except ValueError:
        return False
    fields = ("order_id", "amount_minor", "currency", "action_id")
    return (
        isinstance(data, dict)
        and all(data.get(name) == REFUND[name] for name in fields)
        and type(data.get("amount_minor")) is int
    )


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
@given(results)
def test_never_raises_and_only_business_codes_reject(result: HttpResult) -> None:
    assert rejects_only_business_codes(classify_send, result)


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
@given(results)
def test_only_a_matching_201_confirms(result: HttpResult) -> None:
    assert confirms_only_matching_201(classify_send, result)


def test_generator_reaches_rejected_and_confirmed() -> None:
    # Otherwise the two properties above could pass without ever seeing those outcomes.
    find(results, lambda r: classify_send(r, SENT).kind is SendKind.REJECTED)
    find(results, lambda r: classify_send(r, SENT).kind is SendKind.CONFIRMED)


def invalid_request_rejects(result: HttpResult, sent: SentRefund) -> SendOutcome:
    """The ADR-010 bug, planted: invalid_request treated as a business rejection."""
    if result.status == 400 and b"invalid_request" in result.body:
        return SendOutcome(SendKind.REJECTED, "planted bug")
    return classify_send(result, sent)


def trusts_any_201(result: HttpResult, sent: SentRefund) -> SendOutcome:
    """Planted: any 201 is a confirmation, whatever its body says."""
    if result.status == 201:
        return SendOutcome(SendKind.CONFIRMED, "planted bug")
    return classify_send(result, sent)


def test_control_property_catches_invalid_request_rejecting() -> None:
    find(results, lambda r: not rejects_only_business_codes(invalid_request_rejects, r))


def test_control_property_catches_trusting_any_201() -> None:
    find(results, lambda r: not confirms_only_matching_201(trusts_any_201, r))


def test_real_classifier_has_no_counterexample_for_the_control_searches() -> None:
    with pytest.raises(NoSuchExample):
        find(results, lambda r: not rejects_only_business_codes(classify_send, r))


# --- the reconcile lookup -------------------------------------------------------------------


def lookup(status: int | None, payload: Any = None) -> Any:
    raw = b"" if payload is None else body(payload)
    return classify_lookup(HttpResult(status=status, body=raw), SENT)


LOOKUPS: list[tuple[str, int | None, Any, LookupKind, bool]] = [
    ("empty list", 200, {"refunds": []}, LookupKind.NOT_FOUND, False),
    ("one refund", 200, {"refunds": [REFUND]}, LookupKind.FOUND, False),
    ("two refunds", 200, {"refunds": [REFUND, REFUND | {"id": "other"}]}, LookupKind.FOUND, True),
    (
        "other action's row",
        200,
        {"refunds": [REFUND | {"action_id": "act_2"}]},
        LookupKind.LOOKUP_FAILED,
        True,
    ),
    ("no response", None, None, LookupKind.LOOKUP_FAILED, True),
    ("500", 500, None, LookupKind.LOOKUP_FAILED, True),
    ("401", 401, {"status": "unauthorized"}, LookupKind.LOOKUP_FAILED, True),
    ("400", 400, {"code": "invalid_request"}, LookupKind.LOOKUP_FAILED, True),
    ("200 malformed", 200, {"refund": []}, LookupKind.LOOKUP_FAILED, True),
    ("200 rows not objects", 200, {"refunds": [1]}, LookupKind.LOOKUP_FAILED, True),
    (
        "other amount",
        200,
        {"refunds": [REFUND | {"amount_minor": 999}]},
        LookupKind.LOOKUP_FAILED,
        True,
    ),
    (
        "other currency",
        200,
        {"refunds": [REFUND | {"currency": "EUR"}]},
        LookupKind.LOOKUP_FAILED,
        True,
    ),
    (
        "other order",
        200,
        {"refunds": [REFUND | {"order_id": "ord_2"}]},
        LookupKind.LOOKUP_FAILED,
        True,
    ),
]


@pytest.mark.parametrize(
    ("status", "payload", "kind", "alert"),
    [pytest.param(*row[1:], id=row[0]) for row in LOOKUPS],
)
def test_lookup_table(status: int | None, payload: Any, kind: LookupKind, alert: bool) -> None:
    outcome = lookup(status, payload)
    assert (outcome.kind, outcome.alert) == (kind, alert)


@settings(max_examples=400)
@given(results)
def test_an_error_is_never_not_found(result: HttpResult) -> None:
    outcome = classify_lookup(result, SENT)
    if outcome.kind is LookupKind.NOT_FOUND:
        assert result.status == 200
        assert (
            json.loads(result.body) == {"refunds": []} or json.loads(result.body)["refunds"] == []
        )


# --- the retry schedule ---------------------------------------------------------------------

POLICY = RetryPolicy(
    attempts=5,
    backoff_base=timedelta(seconds=0.5),
    backoff_cap=timedelta(seconds=8),
    client_timeout=timedelta(seconds=8),
)


def test_ceilings_double_until_the_cap() -> None:
    assert [POLICY.ceiling(n).total_seconds() for n in range(1, 8)] == [0.5, 1, 2, 4, 8, 8, 8]


def test_worst_case_window_of_the_defaults() -> None:
    # 5 attempts x 8 s timeout + 4 waits, each possibly a Retry-After capped at 8 s.
    assert POLICY.worst_case_window() == timedelta(seconds=72)


hint_sequences = st.lists(
    st.tuples(
        st.floats(min_value=0, max_value=0.999999),
        st.none() | st.integers(min_value=0, max_value=10_000).map(lambda s: timedelta(seconds=s)),
    ),
    min_size=POLICY.attempts - 1,
    max_size=POLICY.attempts - 1,
)


def total_waits(sequence: list[tuple[float, timedelta | None]]) -> timedelta:
    return sum(
        (
            POLICY.wait_before_retry(n, jitter, retry_after=hint)
            for n, (jitter, hint) in enumerate(sequence, start=1)
        ),
        timedelta(0),
    )


@given(hint_sequences)
def test_no_schedule_of_hints_exceeds_the_worst_case(
    sequence: list[tuple[float, timedelta | None]],
) -> None:
    assert POLICY.client_timeout * POLICY.attempts + total_waits(sequence) <= (
        POLICY.worst_case_window()
    )


def test_control_the_old_formula_is_exceeded() -> None:
    # The first draft summed the early ceilings (0.5 + 1 + 2 + 4 = 7.5 s of waiting).
    old_waits = sum((POLICY.ceiling(n) for n in range(1, POLICY.attempts)), timedelta(0))
    find(hint_sequences, lambda sequence: total_waits(sequence) > old_waits)


@given(st.integers(min_value=1, max_value=50), st.floats(min_value=0, max_value=0.999999))
def test_full_jitter_stays_inside_the_ceiling(retry_number: int, jitter: float) -> None:
    wait = POLICY.wait_before_retry(retry_number, jitter)
    assert timedelta(0) <= wait <= POLICY.ceiling(retry_number)


@pytest.mark.parametrize(
    ("hint", "expected"),
    [(timedelta(seconds=3), timedelta(seconds=3)), (timedelta(hours=1), timedelta(seconds=8))],
    ids=["honored", "capped"],
)
def test_retry_after_replaces_the_draw_but_is_capped(hint: timedelta, expected: timedelta) -> None:
    assert POLICY.wait_before_retry(1, 0.5, retry_after=hint) == expected


@pytest.mark.parametrize(
    "kwargs",
    [{"attempts": 0}, {"backoff_base": timedelta(0)}, {"backoff_base": timedelta(seconds=9)}],
    ids=["no-attempts", "zero-base", "base-over-cap"],
)
def test_bad_policy_is_refused(kwargs: dict[str, Any]) -> None:
    fields: dict[str, Any] = {
        "attempts": 5,
        "backoff_base": timedelta(seconds=0.5),
        "backoff_cap": timedelta(seconds=8),
        "client_timeout": timedelta(seconds=8),
    } | kwargs
    with pytest.raises(ValueError):
        RetryPolicy(**fields)


# --- strict parsing (ADR-012 Part 1) ---------------------------------------------------------

BASE = '"id":"r1","order_id":"ord_1","currency":"USD","action_id":"act_1"'


def raw_refund(fields: str) -> str:
    """A 201 body written by hand, so it can contain what json.dumps never would."""
    return "{" + BASE + "," + fields + "}"


STRICT_CASES = {
    "duplicate key, last value matches": raw_refund('"amount_minor":999,"amount_minor":1000'),
    "duplicate key in a nested object": raw_refund('"amount_minor":1000,"x":{"a":1,"a":2}'),
    "NaN amount": raw_refund('"amount_minor":NaN'),
    "Infinity elsewhere": raw_refund('"amount_minor":1000,"x":Infinity'),
    "bool amount": raw_refund('"amount_minor":true'),
    "float amount": raw_refund('"amount_minor":1000.0'),
}


@pytest.mark.parametrize("name", sorted(STRICT_CASES))
def test_strictly_invalid_201_never_confirms(name: str) -> None:
    outcome = classify_send(HttpResult(201, STRICT_CASES[name].encode()), SENT)
    assert outcome.kind is SendKind.RECONCILE and outcome.alert


def test_oversized_201_never_confirms() -> None:
    padded = REFUND | {"padding": "x" * RESPONSE_SIZE_CAP_BYTES}
    assert classify_send(HttpResult(201, body(padded)), SENT).kind is SendKind.RECONCILE


def test_control_plain_json_would_confirm_the_duplicate_key_body() -> None:
    # Plain json.loads keeps the last duplicate: re-serialized, the ambiguous body confirms.
    plain = json.loads(STRICT_CASES["duplicate key, last value matches"])
    assert classify_send(HttpResult(201, body(plain)), SENT).kind is SendKind.CONFIRMED


# --- the per-send age check (ADR-012 Part 3, ADR-013) ---------------------------------------

SECOND = timedelta(seconds=1)


@pytest.mark.parametrize(
    ("elapsed", "snapshot", "window_read", "allowed"),
    [
        (0, 72, 72, True),
        (64, 72, 72, True),  # 64 + 8 = 72: exactly fits
        (65, 72, 72, False),
        (10, 72, 30, True),  # a shrunk window applies at once
        (25, 72, 30, False),
    ],
    ids=["fresh", "exact-fit", "one-second-late", "shrunk-fits", "shrunk-too-late"],
)
def test_age_check(elapsed: int, snapshot: int, window_read: int, allowed: bool) -> None:
    result = send_allowed(elapsed * SECOND, 8 * SECOND, snapshot * SECOND, window_read * SECOND)
    assert result is allowed


def test_a_grown_window_doesnt_lengthen_an_old_run() -> None:
    # Snapshot 300 s at the first send; the provider now reports 600 s; the run is 400 s old.
    assert send_allowed(400 * SECOND, 8 * SECOND, 300 * SECOND, 600 * SECOND) is False


def test_control_checking_only_the_current_window_would_send() -> None:
    elapsed, timeout, current = 400 * SECOND, 8 * SECOND, 600 * SECOND
    assert elapsed + timeout <= current  # what a current-window-only check would allow


# --- reading the provider config (ADR-013) ---------------------------------------------------

CAP = timedelta(minutes=15)
TIMEOUT = 8 * SECOND
GOOD_CONFIG = {
    "max_retry_window_ms": 600_000,
    "key_retention_ms": 86_400_000,
    "processing_budget_ms": 5_000,
}


def raw_config(window: str, retention: str = "86400000", budget: str = "5000") -> str:
    """A config body written by hand (duplicate keys, NaN and floats can't come from dumps)."""
    fields = f'"max_retry_window_ms":{window},"key_retention_ms":{retention}'
    return "{" + fields + f',"processing_budget_ms":{budget}' + "}"


def read(status: int | None, payload: Any) -> Any:
    raw = payload.encode() if isinstance(payload, str) else body(payload)
    return read_provider_config(HttpResult(status, raw), TIMEOUT, CAP)


def test_a_valid_config_is_read_in_milliseconds() -> None:
    result = read(200, GOOD_CONFIG)
    assert result.kind is ConfigReadKind.OK and not result.alert
    assert result.config.max_retry_window == timedelta(minutes=10)
    assert result.config.key_retention == timedelta(days=1)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_pages(status: int) -> None:
    result = read(status, {"status": "unauthorized"})
    assert result.kind is ConfigReadKind.AUTH_FAILED and result.alert


BAD_CONFIGS: dict[str, Any] = {
    "retention-not-above-window-plus-timeout": GOOD_CONFIG | {"key_retention_ms": 608_000},
    "window-above-the-agent-cap": GOOD_CONFIG
    | {"max_retry_window_ms": 1_000_000_000, "key_retention_ms": 2_000_000_000},
    "float-window": raw_config("600000.0"),
    "bool-window": GOOD_CONFIG | {"max_retry_window_ms": True},
    "zero-window": GOOD_CONFIG | {"max_retry_window_ms": 0},
    "missing-field": {k: v for k, v in GOOD_CONFIG.items() if k != "key_retention_ms"},
    "duplicate-window": raw_config('1,"max_retry_window_ms":600000'),
    "nan-window": raw_config("NaN"),
}


@pytest.mark.parametrize("name", sorted(BAD_CONFIGS))
def test_an_invalid_config_is_a_failed_read(name: str) -> None:
    result = read(200, BAD_CONFIGS[name])
    assert result.kind is ConfigReadKind.FAILED and result.alert and result.config is None


@pytest.mark.parametrize("status", [None, 500, 503, 404])
def test_an_unreachable_config_is_a_failed_read(status: int | None) -> None:
    assert read(status, {}).kind is ConfigReadKind.FAILED


def test_control_trusting_the_value_would_accept_an_absurd_window() -> None:
    naive = json.loads(body(BAD_CONFIGS["window-above-the-agent-cap"]))
    assert timedelta(milliseconds=naive["max_retry_window_ms"]) > CAP

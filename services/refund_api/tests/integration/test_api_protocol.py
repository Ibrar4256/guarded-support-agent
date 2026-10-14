"""The idempotency protocol over HTTP (ADR-004, ADR-010), against real Postgres.

In-process (httpx ASGITransport): these prove the server's logic. A real hung connection,
and so real proof of timeout_after_commit handling, needs tests/e2e (ADR-010).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from conftest import NEXT_TOKEN, TOKEN, api_client, api_settings, create_charge, refund_rows, unique
from httpx import AsyncClient, Response
from sqlalchemy import Engine, text

from refund_api import service
from refund_api.hooks import RefundHooks
from refund_api.service import REPLAYED_HEADER

pytestmark = pytest.mark.usefixtures("retention_floor")
AMOUNT = 1_000
WAIT_LIMIT_SECONDS = 5.0


def refund_body(order_id: str, action_id: str, amount: int = AMOUNT, currency: str = "USD") -> Any:
    return {
        "order_id": order_id,
        "amount_minor": amount,
        "currency": currency,
        "action_id": action_id,
    }


def headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": key}


async def post(client: AsyncClient, key: str, body: Any) -> Response:
    return await client.post("/refunds", json=body, headers=headers(key))


class PauseBeforeCommit(RefundHooks):
    """Holds one key's transaction open, with its locks, until released."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def before_commit(self, idempotency_key: str) -> None:
        if idempotency_key == self.key:
            self.reached.set()
            await self.release.wait()


async def wait_until(condition: Callable[[], Awaitable[bool]]) -> None:
    deadline = time.monotonic() + WAIT_LIMIT_SECONDS
    while not await condition():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.05)


# --- create, replay, mismatch -------------------------------------------------------------


async def test_replay_is_byte_identical_and_creates_nothing(runtime_engine: Engine) -> None:
    order, action, key = create_charge(runtime_engine, AMOUNT), unique("act"), unique("key")
    async with api_client() as client:
        first = await post(client, key, refund_body(order, action))
        again = await post(client, key, refund_body(order, action))
    assert first.status_code == 201 and REPLAYED_HEADER not in first.headers
    assert again.status_code == 201 and again.headers[REPLAYED_HEADER] == "true"
    assert again.content == first.content
    assert refund_rows(runtime_engine, action_id=action) == 1


async def test_same_key_different_payload_is_422(runtime_engine: Engine) -> None:
    order, action, key = create_charge(runtime_engine, AMOUNT), unique("act"), unique("key")
    async with api_client() as client:
        assert (await post(client, key, refund_body(order, action))).status_code == 201
        changed = await post(client, key, refund_body(order, action, amount=AMOUNT + 1))
    assert changed.status_code == 422
    assert changed.json() == {"status": "key_payload_mismatch"}
    assert refund_rows(runtime_engine, action_id=action) == 1


# --- business errors ----------------------------------------------------------------------


async def test_business_errors_store_no_key(runtime_engine: Engine) -> None:
    # charge_not_found, then the charge appears: the same key must create, not replay a 400.
    order, action, key = unique("ord"), unique("act"), unique("key")
    async with api_client() as client:
        missing = await post(client, key, refund_body(order, action))
        assert missing.status_code == 400
        assert missing.json()["code"] == "charge_not_found"
        with runtime_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO charges (order_id, amount_minor, currency) VALUES (:o, :a, 'USD')"
                ),
                {"o": order, "a": 2 * AMOUNT},
            )
        created = await post(client, key, refund_body(order, action))
    assert created.status_code == 201 and REPLAYED_HEADER not in created.headers


@pytest.mark.parametrize(
    ("charge", "currency", "code"),
    [(AMOUNT - 1, "USD", "refund_exceeds_charge"), (2 * AMOUNT, "EUR", "currency_mismatch")],
)
async def test_business_error_codes(
    runtime_engine: Engine, charge: int, currency: str, code: str
) -> None:
    order = create_charge(runtime_engine, AMOUNT, charge_amount_minor=charge)
    action = unique("act")
    async with api_client() as client:
        response = await post(client, unique("key"), refund_body(order, action, currency=currency))
    assert response.status_code == 400
    assert response.json() == {"code": code, "fields": []}
    assert refund_rows(runtime_engine, action_id=action) == 0


async def test_exceeds_charge_counts_every_earlier_refund(runtime_engine: Engine) -> None:
    order = create_charge(runtime_engine, AMOUNT, charge_amount_minor=AMOUNT + AMOUNT // 2)
    async with api_client() as client:
        first = await post(client, unique("key"), refund_body(order, unique("act")))
        second = await post(client, unique("key"), refund_body(order, unique("act")))
    assert first.status_code == 201
    assert second.json()["code"] == "refund_exceeds_charge"


# --- the reconcile lookup -----------------------------------------------------------------


async def test_lookup_lists_every_refund_for_the_action(runtime_engine: Engine) -> None:
    order, action = create_charge(runtime_engine, AMOUNT), unique("act")
    query = {"order_id": order, "action_id": action}
    auth = {"Authorization": f"Bearer {TOKEN}"}
    async with api_client() as client:
        before = await client.get("/refunds", params=query, headers=auth)
        await post(client, unique("key"), refund_body(order, action))
        after = await client.get("/refunds", params=query, headers=auth)
    assert before.status_code == 200 and before.json() == {"refunds": []}
    assert [refund["action_id"] for refund in after.json()["refunds"]] == [action]


# --- the hazards ADR-010 records ----------------------------------------------------------


async def test_malformed_retry_of_a_committed_key_is_400_while_the_refund_exists(
    runtime_engine: Engine,
) -> None:
    # Why the agent must treat invalid_request as unknown (ADR-010 Amends 1): validation
    # runs before the key lookup, so this 400 says nothing about the committed attempt.
    order, action, key = create_charge(runtime_engine, AMOUNT), unique("act"), unique("key")
    async with api_client() as client:
        assert (await post(client, key, refund_body(order, action))).status_code == 201
        malformed = await post(client, key, {**refund_body(order, action), "amount_minor": "x"})
    assert malformed.status_code == 400
    assert malformed.json()["code"] == "invalid_request"
    assert refund_rows(runtime_engine, action_id=action) == 1


async def test_expired_key_resent_creates_a_second_refund(runtime_engine: Engine) -> None:
    # Why the worker never re-sends past retention (ADR-008): the provider sees a new key.
    short = api_settings(
        key_retention=timedelta(seconds=2),
        max_retry_window=timedelta(milliseconds=500),
        max_recovery_delay=timedelta(milliseconds=500),
    )
    order, action, key = create_charge(runtime_engine, AMOUNT), unique("act"), unique("key")
    async with api_client(short) as client:
        assert (await post(client, key, refund_body(order, action))).status_code == 201
        await asyncio.sleep(2.5)
        resent = await post(client, key, refund_body(order, action))
    assert resent.status_code == 201 and REPLAYED_HEADER not in resent.headers
    assert refund_rows(runtime_engine, action_id=action) == 2


# --- concurrency --------------------------------------------------------------------------


async def test_same_key_in_flight_is_409_then_replays(runtime_engine: Engine) -> None:
    order, action, key = create_charge(runtime_engine, AMOUNT), unique("act"), unique("key")
    hooks = PauseBeforeCommit(key)
    async with api_client(hooks=hooks) as client:
        first = asyncio.create_task(post(client, key, refund_body(order, action)))
        await asyncio.wait_for(hooks.reached.wait(), WAIT_LIMIT_SECONDS)
        in_flight = await post(client, key, refund_body(order, action))
        hooks.release.set()
        committed = await first
        retry = await post(client, key, refund_body(order, action))
    assert in_flight.status_code == 409 and in_flight.json() == {"status": "in_flight"}
    assert committed.status_code == 201
    assert retry.status_code == 201 and retry.headers[REPLAYED_HEADER] == "true"
    assert refund_rows(runtime_engine, action_id=action) == 1


class CollidingLocks(PauseBeforeCommit):
    def lock_key(self, idempotency_key: str) -> int:
        return 42


async def test_lock_collision_is_a_spurious_409_that_succeeds_on_retry(
    runtime_engine: Engine,
) -> None:
    key_a, key_b = unique("key"), unique("key")
    order_a, order_b = create_charge(runtime_engine, AMOUNT), create_charge(runtime_engine, AMOUNT)
    hooks = CollidingLocks(key_a)
    async with api_client(hooks=hooks) as client:
        first = asyncio.create_task(post(client, key_a, refund_body(order_a, unique("act"))))
        await asyncio.wait_for(hooks.reached.wait(), WAIT_LIMIT_SECONDS)
        collided = await post(client, key_b, refund_body(order_b, unique("act")))
        hooks.release.set()
        await first
        retried = await post(client, key_b, refund_body(order_b, unique("act")))
    assert collided.status_code == 409
    assert retried.status_code == 201


async def race_two_keys_on_one_charge(
    runtime_engine: Engine, owner_engine: Engine
) -> tuple[Response, Response, str]:
    """Key A pauses with its refund written; key B arrives for the same charge."""
    order = create_charge(runtime_engine, AMOUNT, charge_amount_minor=AMOUNT + AMOUNT // 2)
    key_a = unique("key")
    hooks = PauseBeforeCommit(key_a)

    async def someone_waits_on_a_lock() -> bool:
        def count() -> int:
            with owner_engine.connect() as connection:
                waiting: int = connection.execute(
                    text("SELECT count(*) FROM pg_locks WHERE NOT granted")
                ).scalar_one()
            return waiting

        return await asyncio.to_thread(count) > 0

    async with api_client(hooks=hooks) as client:
        first = asyncio.create_task(post(client, key_a, refund_body(order, unique("act"))))
        await asyncio.wait_for(hooks.reached.wait(), WAIT_LIMIT_SECONDS)
        second = asyncio.create_task(post(client, unique("key"), refund_body(order, unique("act"))))

        async def blocked_or_done() -> bool:
            return second.done() or await someone_waits_on_a_lock()

        await wait_until(blocked_or_done)
        hooks.release.set()
        return await first, await second, order


async def test_concurrent_keys_on_one_charge_only_one_succeeds(
    runtime_engine: Engine, owner_engine: Engine
) -> None:
    first, second, order = await race_two_keys_on_one_charge(runtime_engine, owner_engine)
    assert first.status_code == 201
    assert second.status_code == 400 and second.json()["code"] == "refund_exceeds_charge"
    assert refund_rows(runtime_engine, order_id=order) == 1


async def test_control_without_the_charge_lock_both_succeed(
    runtime_engine: Engine, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unlocked(connection: Any, order_id: str) -> Any:
        query = text("SELECT * FROM charges WHERE order_id = :o")
        return (await connection.execute(query, {"o": order_id})).one_or_none()

    monkeypatch.setattr(service, "lock_charge", unlocked)
    first, second, order = await race_two_keys_on_one_charge(runtime_engine, owner_engine)
    assert (first.status_code, second.status_code) == (201, 201)
    assert refund_rows(runtime_engine, order_id=order) == 2


# --- the processing deadline --------------------------------------------------------------

TIGHT = {
    "processing_budget": timedelta(milliseconds=500),
    "pool_timeout": timedelta(milliseconds=200),
}


class DelayBeforeTransaction(RefundHooks):
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def before_transaction(self, idempotency_key: str) -> None:
        await asyncio.sleep(self.seconds)


class DelayBeforeCommit(RefundHooks):
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def before_commit(self, idempotency_key: str) -> None:
        await asyncio.sleep(self.seconds)


async def send_with(runtime_engine: Engine, hooks: RefundHooks) -> tuple[Response, str]:
    order, action = create_charge(runtime_engine, AMOUNT), unique("act")
    async with api_client(api_settings(**TIGHT), hooks) as client:
        return await post(client, unique("key"), refund_body(order, action)), action


async def test_time_before_the_transaction_counts_against_the_budget(
    runtime_engine: Engine,
) -> None:
    response, action = await send_with(runtime_engine, DelayBeforeTransaction(0.6))
    assert response.status_code == 503 and response.json() == {"status": "deadline_exceeded"}
    assert refund_rows(runtime_engine, action_id=action) == 0


async def test_control_anchored_at_transaction_start_it_would_commit(
    runtime_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first draft's bug: ignore time since receipt. Then the same delay commits.
    monkeypatch.setattr(
        service, "remaining_budget", lambda settings, _: settings.processing_budget.total_seconds()
    )
    response, action = await send_with(runtime_engine, DelayBeforeTransaction(0.6))
    assert response.status_code == 201
    assert refund_rows(runtime_engine, action_id=action) == 1


async def test_commit_after_the_deadline_rolls_back(runtime_engine: Engine) -> None:
    response, action = await send_with(runtime_engine, DelayBeforeCommit(0.6))
    assert response.status_code == 503
    assert refund_rows(runtime_engine, action_id=action) == 0


async def test_control_within_the_budget_commits(runtime_engine: Engine) -> None:
    response, action = await send_with(runtime_engine, DelayBeforeCommit(0.05))
    assert response.status_code == 201
    assert refund_rows(runtime_engine, action_id=action) == 1


# --- authentication with a real refund ----------------------------------------------------


async def test_the_next_rotation_token_is_accepted(runtime_engine: Engine) -> None:
    order, action = create_charge(runtime_engine, AMOUNT), unique("act")
    async with api_client() as client:
        response = await client.post(
            "/refunds",
            json=refund_body(order, action),
            headers={"Authorization": f"Bearer {NEXT_TOKEN}", "Idempotency-Key": unique("key")},
        )
    assert response.status_code == 201

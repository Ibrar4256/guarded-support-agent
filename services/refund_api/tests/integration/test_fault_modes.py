"""Every refund-api failure mode (ADR-004), through the fault-injection build (ADR-011).

Each test asserts its fault actually fired: a fault that silently didn't apply would let
the test pass in normal mode (ADR-010 Part 3). In-process: logic only. The real hung
connection of timeout_after_commit is proven in tests/e2e.
"""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from conftest import AUTH, Faults, api_settings, charge_via_api, fault_client, refund_rows, unique
from httpx import AsyncClient, Response
from sqlalchemy import Engine

from refund_api.service import REPLAYED_HEADER

pytestmark = pytest.mark.usefixtures("retention_floor")
AMOUNT = 1_000
INJECTED = {"status": "injected_fault"}
WAIT_LIMIT_SECONDS = 5.0


def body(order_id: str, action_id: str) -> dict[str, Any]:
    return {"order_id": order_id, "amount_minor": AMOUNT, "currency": "USD", "action_id": action_id}


async def post(client: AsyncClient, key: str, order_id: str, action_id: str) -> Response:
    return await client.post(
        "/refunds", json=body(order_id, action_id), headers=AUTH | {"Idempotency-Key": key}
    )


async def wait_until_applied(faults: Faults, action_id: str, fault: str) -> None:
    """Bounded: if the fault never fires, fail instead of hanging the suite."""
    deadline = asyncio.get_running_loop().time() + WAIT_LIMIT_SECONDS
    while fault not in await faults.applied(action_id):
        assert asyncio.get_running_loop().time() < deadline, f"{fault} never fired"
        await asyncio.sleep(0.02)


async def setup(client: AsyncClient) -> tuple[str, str, str]:
    return await charge_via_api(client, AMOUNT), unique("act"), unique("key")


async def test_fail_before_commit_writes_nothing_and_the_retry_is_safe(
    runtime_engine: Engine,
) -> None:
    async with fault_client() as (client, faults):
        order, action, key = await setup(client)
        await faults.plan(action, "fail_before_commit")
        failed = await post(client, key, order, action)
        await faults.assert_applied(action, "fail_before_commit")
        assert refund_rows(runtime_engine, action_id=action) == 0
        retry = await post(client, key, order, action)
    assert (failed.status_code, failed.json()) == (500, INJECTED)
    assert retry.status_code == 201 and REPLAYED_HEADER not in retry.headers
    assert refund_rows(runtime_engine, action_id=action) == 1


async def test_fail_after_commit_replays_the_original(runtime_engine: Engine) -> None:
    async with fault_client() as (client, faults):
        order, action, key = await setup(client)
        await faults.plan(action, "fail_after_commit")
        failed = await post(client, key, order, action)
        await faults.assert_applied(action, "fail_after_commit")
        retry = await post(client, key, order, action)
    assert failed.status_code == 500
    assert retry.status_code == 201 and retry.headers[REPLAYED_HEADER] == "true"
    assert refund_rows(runtime_engine, action_id=action) == 1


async def test_invalid_request_after_commit_commits_then_answers_like_a_real_one(
    runtime_engine: Engine,
) -> None:
    """The agent sees refund-api's own invalid_request body, though the refund committed; the
    retry replays the commit (ADR-010, ADR-016's e2e)."""
    async with fault_client() as (client, faults):
        order, action, key = await setup(client)
        real = await client.post(
            "/refunds",
            json=body(order, action) | {"amount_minor": "ten"},
            headers=AUTH | {"Idempotency-Key": unique("key")},
        )
        await faults.plan(action, "invalid_request_after_commit")
        injected = await post(client, key, order, action)
        await faults.assert_applied(action, "invalid_request_after_commit")
        assert refund_rows(runtime_engine, action_id=action) == 1, "committed"
        retry = await post(client, key, order, action)
    assert (real.status_code, injected.status_code) == (400, 400)
    assert injected.json() == real.json() | {"fields": []}
    assert retry.status_code == 201 and retry.headers[REPLAYED_HEADER] == "true"
    assert refund_rows(runtime_engine, action_id=action) == 1


async def test_timeout_after_commit_is_committed_not_failed(runtime_engine: Engine) -> None:
    # Logic only: the client gives up; the refund is already committed; a retry replays.
    async with fault_client() as (client, faults):
        order, action, key = await setup(client)
        await faults.plan(action, "timeout_after_commit", seconds=5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(post(client, key, order, action), timeout=0.5)
        await faults.assert_applied(action, "timeout_after_commit")
        assert refund_rows(runtime_engine, action_id=action) == 1
        retry = await post(client, key, order, action)
    assert retry.status_code == 201 and retry.headers[REPLAYED_HEADER] == "true"
    assert refund_rows(runtime_engine, action_id=action) == 1


async def test_slow_commit_a_lookup_during_the_delay_finds_nothing(
    runtime_engine: Engine,
) -> None:
    # Why reconciliation must wait out a quiet period before concluding "not found".
    async with fault_client() as (client, faults):
        order, action, key = await setup(client)
        await faults.plan(action, "slow_commit", seconds=1)
        sending = asyncio.create_task(post(client, key, order, action))
        await wait_until_applied(faults, action, "slow_commit")
        query = {"order_id": order, "action_id": action}
        during = await client.get("/refunds", params=query, headers=AUTH)
        sent = await sending
        after = await client.get("/refunds", params=query, headers=AUTH)
    assert during.json() == {"refunds": []}
    assert sent.status_code == 201
    assert len(after.json()["refunds"]) == 1


async def test_dedup_disabled_stays_on_across_the_retry(runtime_engine: Engine) -> None:
    # The negative control the crash tests rely on: the retry must NOT deduplicate.
    async with fault_client() as (client, faults):
        order, action, key = await setup(client)
        await faults.plan(action, "dedup_disabled")
        first = await post(client, key, order, action)
        second = await post(client, key, order, action)
        assert await faults.applied(action) == ["dedup_disabled", "dedup_disabled"]
    assert first.status_code == second.status_code == 201
    assert REPLAYED_HEADER not in second.headers
    assert refund_rows(runtime_engine, action_id=action) == 2


async def test_one_shot_fault_is_used_up(runtime_engine: Engine) -> None:
    async with fault_client() as (client, faults):
        order, action, key = await setup(client)
        await faults.plan(action, "fail_before_commit")
        await post(client, key, order, action)
        state = (await client.get(f"/_test/faults/{action}", headers=AUTH)).json()
    assert state["used"] is True
    assert state["applied"] == ["fail_before_commit"]


async def test_retention_override_applies_to_one_action_only(runtime_engine: Engine) -> None:
    # Per-action retention (the process-wide config stays at one hour); the floor is 1 s.
    async with fault_client() as (client, faults):
        order, action, key = await setup(client)
        other_order, other_action, other_key = await setup(client)
        await faults.plan(action, "normal", key_retention_seconds=1)
        await post(client, key, order, action)
        await post(client, other_key, other_order, other_action)
        await asyncio.sleep(1.5)
        resent = await post(client, key, order, action)
        other_resent = await post(client, other_key, other_order, other_action)
        await faults.assert_applied(action, "key_retention_override")
    assert REPLAYED_HEADER not in resent.headers
    assert refund_rows(runtime_engine, action_id=action) == 2
    assert other_resent.headers[REPLAYED_HEADER] == "true"
    assert refund_rows(runtime_engine, action_id=other_action) == 1


async def test_control_a_plan_for_another_action_is_caught_as_not_applied(
    runtime_engine: Engine,
) -> None:
    # The applied-check is what stops a mis-targeted fault from passing in normal mode.
    async with fault_client() as (client, faults):
        order, action, key = await setup(client)
        wrong = unique("act")
        await faults.plan(wrong, "fail_before_commit")
        response = await post(client, key, order, action)
        assert response.status_code == 201
        with pytest.raises(AssertionError, match="never fired"):
            await faults.assert_applied(wrong, "fail_before_commit")


async def test_teardown_removes_only_its_own_plans() -> None:
    async with fault_client() as (client, _):
        first, second = Faults(client), Faults(client)
        mine, theirs = unique("act"), unique("act")
        await first.plan(mine, "dedup_disabled")
        await second.plan(theirs, "dedup_disabled")
        await first.remove_mine()
        assert (await client.get(f"/_test/faults/{mine}", headers=AUTH)).json()["mode"] is None
        still = (await client.get(f"/_test/faults/{theirs}", headers=AUTH)).json()
        await second.remove_mine()
    assert still["mode"] == "dedup_disabled"


async def test_charge_endpoint_refuses_a_duplicate() -> None:
    async with fault_client() as (client, _):
        order = await charge_via_api(client, AMOUNT)
        again = await client.post(
            "/_test/charges",
            json={"order_id": order, "amount_minor": 1, "currency": "USD"},
            headers=AUTH,
        )
    assert again.status_code == 409


@pytest.mark.parametrize("mode", ["timeout_after_commit", "slow_commit"])
async def test_timed_modes_need_seconds(mode: str) -> None:
    async with fault_client() as (client, _):
        response = await client.post(
            "/_test/faults", json={"action_id": unique("act"), "mode": mode}, headers=AUTH
        )
    assert response.status_code == 400


async def test_test_routes_need_the_service_token() -> None:
    async with fault_client() as (client, _):
        response = await client.post("/_test/reset")
    assert response.status_code == 401


async def test_short_budget_settings_still_apply_in_the_fault_build(
    runtime_engine: Engine,
) -> None:
    # slow_commit past the deadline rolls back: fault plans don't bypass ADR-010's deadline.
    tight = api_settings(
        processing_budget=timedelta(milliseconds=500), pool_timeout=timedelta(milliseconds=200)
    )
    async with fault_client(tight) as (client, faults):
        order, action, key = await setup(client)
        await faults.plan(action, "slow_commit", seconds=0.8)
        response = await post(client, key, order, action)
        await faults.assert_applied(action, "slow_commit")
    assert response.status_code == 503
    assert refund_rows(runtime_engine, action_id=action) == 0

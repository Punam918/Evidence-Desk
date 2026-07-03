import asyncio
from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from evidencedesk.billing.service import BillingService
from evidencedesk.chat.models import Answer
from evidencedesk.db import usage_repository
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext, authorized_transaction, engine, tenant_transaction
from evidencedesk.errors import Problem
from evidencedesk.providers.base import (
    ModelFault,
    ModelRequest,
    ModelResult,
    Prices,
    TextDelta,
    TokenCount,
    Usage,
)

from tests.support import headers, sign_in

pytestmark = pytest.mark.integration
PRICES = Prices("fixture-v1", Decimal(".1"), Decimal(".5"), Decimal(".1"))
REQUEST = ModelRequest("paid_standard", "fixture-model", "Cite sources.", "Question")
RESULT = ModelResult(
    "completed",
    Answer(status="abstained", sentences=[], limitations=["No evidence."]),
    Usage(input_tokens=100, output_tokens=20),
    "fixture-resolved",
    "resp_fixture",
    "{}",
)


class Adapter:
    calls = 0

    def estimate(self, request):
        return TokenCount(1000, True, "fixture")

    async def stream(self, request):
        self.calls += 1
        yield TextDelta("draft")
        yield RESULT


@pytest.fixture
async def billing(auth_client, database):
    client, state = auth_client
    identity = sign_in(client, state)
    h = headers(identity)
    workspace = client.post("/v1/workspaces", json={"name": "Budgets"}, headers=h).json()["id"]
    kb = uuid4()
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.knowledge_bases(workspace_id,id,name,created_by) VALUES(%s,%s,%s,%s)",
            (workspace, kb, "Sources", identity["user"]["id"]),
        )
    base = f"/v1/workspaces/{workspace}"
    for i in range(2):
        conversation = client.post(
            base + "/conversations",
            json={"title": f"Budget {i}"},
            headers={**h, "Idempotency-Key": uuid4().hex},
        ).json()["id"]
        result = client.post(
            base + f"/conversations/{conversation}/runs",
            json={
                "query": "Question",
                "kb_ids": [str(kb)],
                "model_key": "auto",
                "intent": "ask",
                "expected_conversation_version": 1,
            },
            headers={**h, "Idempotency-Key": uuid4().hex},
        )
        assert result.status_code == 202, result.text
    db, dispatch = engine(database["ed_app"]), engine(database["ed_dispatch"])
    ctx = AuthContext(UUID(identity["user"]["id"]), UUID(workspace), "owner", 1, uuid4())
    jobs = JobRepository(dispatch)
    leases = [await jobs.claim("chat", "budget-a"), await jobs.claim("chat", "budget-b")]
    assert all(leases)
    try:
        yield BillingService(db), ctx, leases, db
    finally:
        await db.dispose()
        await dispatch.dispose()


async def test_atomic_concurrent_reservations_respect_workspace_limit(billing, database):
    service, ctx, leases, db = billing
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.workspaces SET daily_budget_micro_usd=1000 WHERE id=%s", (ctx.workspace_id,)
        )
    results = await asyncio.gather(
        *(service.reserve(ctx, lease, REQUEST, Adapter(), PRICES, True) for lease in leases),
        return_exceptions=True,
    )
    assert (
        sum(isinstance(result, Problem) and result.code == "budget_exceeded" for result in results)
        == 1
    )
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM app.model_usage WHERE workspace_id=%s", (ctx.workspace_id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT reserved_micro_usd FROM app.budget_windows WHERE workspace_id=%s AND period='day'",
                (ctx.workspace_id,),
            ).fetchone()[0]
            == 612
        )


async def test_attempt_replay_uses_stored_response_and_settles_once(billing, database):
    service, ctx, leases, _ = billing
    adapter = Adapter()
    result = [
        event
        async for event in service.execute(ctx, leases[0], REQUEST, adapter, PRICES, hosted=True)
    ]
    assert result[-1] == RESULT and adapter.calls == 1
    replay = [
        event
        async for event in service.execute(
            ctx,
            leases[0],
            REQUEST,
            adapter,
            replace(PRICES, version="v2", input=Decimal("1")),
            hosted=True,
        )
    ]
    assert replay == [RESULT] and adapter.calls == 1
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT state,execution_state,cost_micro_usd,price_version FROM app.model_usage WHERE workspace_id=%s",
            (ctx.workspace_id,),
        ).fetchone() == ("settled", "response_stored", 20, "fixture-v1")
        assert conn.execute(
            "SELECT reserved_micro_usd,spent_micro_usd FROM app.budget_windows WHERE workspace_id=%s",
            (ctx.workspace_id,),
        ).fetchall() == [(0, 20), (0, 20)]


async def test_interrupted_stream_is_charged_conservatively_and_cannot_replay(billing, database):
    service, ctx, leases, _ = billing
    adapter = Adapter()
    stream = service.execute(ctx, leases[0], REQUEST, adapter, PRICES, hosted=True)
    assert await anext(stream) == TextDelta("draft")
    await stream.aclose()
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT state,execution_state,cost_micro_usd FROM app.model_usage WHERE workspace_id=%s",
            (ctx.workspace_id,),
        ).fetchone() == ("estimated", "ambiguous", 612)
    with pytest.raises(Problem, match="existing attempt"):
        await service.reserve(ctx, leases[0], REQUEST, adapter, PRICES, True)
    assert adapter.calls == 1


async def test_policy_change_between_reservation_and_issue_blocks_transport(billing, database):
    service, ctx, leases, _ = billing
    adapter = Adapter()
    await service.reserve(ctx, leases[0], REQUEST, adapter, PRICES, True)
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.workspaces SET privacy_mode='local_only' WHERE id=%s", (ctx.workspace_id,)
        )
    with pytest.raises(Problem) as error:
        _ = [
            event
            async for event in service.execute(
                ctx, leases[0], REQUEST, adapter, PRICES, hosted=True
            )
        ]
    assert error.value.code == "privacy_policy" and adapter.calls == 0


async def test_definite_failure_allows_bounded_incremental_retry(billing, database):
    service, ctx, leases, db = billing
    adapter = Adapter()
    for number in range(1, 4):
        attempt = await service.reserve(ctx, leases[0], REQUEST, adapter, PRICES, True)
        assert attempt.number == number
        async with authorized_transaction(db, ctx, write=True) as conn:
            await usage_repository.mark_issued(conn, ctx, leases[0], attempt, True)
        async with tenant_transaction(db, ctx) as conn:
            await usage_repository.settle(
                conn,
                ctx,
                leases[0],
                attempt,
                ModelFault("rate_limit", "definite_failure", True),
                0.1,
            )
    with pytest.raises(Problem) as error:
        await service.reserve(ctx, leases[0], REQUEST, adapter, PRICES, True)
    assert error.value.code == "run_budget_exceeded"
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT sum(cost_micro_usd) FROM app.model_usage WHERE workspace_id=%s",
                (ctx.workspace_id,),
            ).fetchone()[0]
            == 0
        )


async def test_settlement_uses_attempt_window_and_frozen_prices(billing, database):
    service, ctx, leases, db = billing
    attempt = await service.reserve(ctx, leases[0], REQUEST, Adapter(), PRICES, True)
    async with authorized_transaction(db, ctx, write=True) as conn:
        await usage_repository.mark_issued(conn, ctx, leases[0], attempt, True)
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.budget_windows SET starts_at=CASE WHEN period='day' THEN date '2026-01-31' ELSE date '2026-01-01' END WHERE workspace_id=%s",
            (ctx.workspace_id,),
        )
        conn.execute(
            "UPDATE app.model_usage SET day_start='2026-01-31',month_start='2026-01-01' WHERE workspace_id=%s",
            (ctx.workspace_id,),
        )
    async with tenant_transaction(db, ctx) as conn:
        await usage_repository.settle(
            conn,
            ctx,
            leases[0],
            replace(attempt, prices=replace(PRICES, input=Decimal("100"))),
            RESULT,
            0.5,
        )
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT reserved_micro_usd,spent_micro_usd FROM app.budget_windows WHERE workspace_id=%s",
            (ctx.workspace_id,),
        ).fetchall() == [(0, 20), (0, 20)]


async def test_cancel_during_generation_discards_next_emission_but_accounts(billing, database):
    service, ctx, leases, _ = billing
    adapter = Adapter()
    stream = service.execute(ctx, leases[0], REQUEST, adapter, PRICES, hosted=True)
    await anext(stream)
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.runs SET cancel_requested=true WHERE workspace_id=%s AND id=%s",
            (ctx.workspace_id, leases[0].run_id),
        )
    with pytest.raises(Problem) as error:
        await anext(stream)
    assert error.value.code == "run_cancelled"
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT cost_micro_usd FROM app.model_usage WHERE workspace_id=%s",
                (ctx.workspace_id,),
            ).fetchone()[0]
            == 20
        )


async def test_local_slot_is_global_and_unknown_zero_cost_stays_estimated(billing, database):
    service, ctx, leases, _ = billing
    zero = Prices("local-v1", Decimal(0), Decimal(0), Decimal(0))
    request = replace(
        REQUEST, model_key="local_standard", model_id="fixture-local", context_cap=8192
    )
    first = service.execute(ctx, leases[0], request, Adapter(), zero, hosted=False)
    assert await anext(first) == TextDelta("draft")
    try:
        second = service.execute(ctx, leases[1], request, Adapter(), zero, hosted=False)
        with pytest.raises(Problem) as error:
            await anext(second)
        assert error.value.code == "local_model_busy"
    finally:
        await first.aclose()
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT state,usage_estimated,cost_micro_usd FROM app.model_usage WHERE workspace_id=%s",
            (ctx.workspace_id,),
        ).fetchone() == ("estimated", True, 0)
    result = [
        event
        async for event in service.execute(ctx, leases[1], request, Adapter(), zero, hosted=False)
    ]
    assert result[-1] == RESULT

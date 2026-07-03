import asyncio
from uuid import uuid4

import psycopg
import pytest
from evidencedesk.db.session import engine
from evidencedesk.models.health import Capabilities, ProviderHealth

from tests.integration.billing.test_budgets import billing as billing

pytestmark = pytest.mark.integration


async def test_single_probe_owner_stale_fencing_and_capability_failure(database):
    db = engine(database["ed_dispatch"])
    other = engine(database["ed_dispatch"])
    try:
        first, second = ProviderHealth(db), ProviderHealth(other)
        key = "probe_" + uuid4().hex
        claims = await asyncio.gather(
            first.claim_probe(key, "a" * 64, "b" * 64), second.claim_probe(key, "a" * 64, "b" * 64)
        )
        live = [c for c in claims if c]
        assert len(live) == 1
        lease = live[0]
        assert await first.complete_probe(lease, Capabilities(True, True, True, True))
        assert not await first.complete_probe(lease, Capabilities(True, True, True, True))
        assert await first.claim_probe(key, "a" * 64, "b" * 64) is None
        changed = await second.claim_probe(key, "a" * 64, "c" * 64)
        assert changed
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.provider_health SET probe_until=now()-interval '1 second' WHERE model_key=%s",
                (key,),
            )
        takeover = await first.claim_probe(key, "a" * 64, "c" * 64)
        assert takeover
        assert not await second.complete_probe(changed, Capabilities(True, True, True, True))
        assert await first.complete_probe(takeover, Capabilities(True, False, True, True))
        with psycopg.connect(database["operator"]) as conn:
            row = conn.execute(
                "SELECT state,capability_report->>'passed' FROM app.provider_health WHERE model_key=%s",
                (key,),
            ).fetchone()
            assert row == ("open", "false")
        assert await first.claim_probe(key, "a" * 64, "c" * 64) is None
    finally:
        await db.dispose()
        await other.dispose()


async def test_rolling_breaker_counts_settled_attempts_once_and_ignores_stale_config(
    billing, database
):
    # Reuse the real scoped admission fixture, not a bypassed authorization substitute.
    db = engine(database["ed_dispatch"])
    health = ProviderHealth(db)
    try:
        with psycopg.connect(database["operator"]) as conn:
            workspace, user = conn.execute(
                "SELECT workspace_id,user_id FROM app.runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        key = "breaker_" + uuid4().hex
        probe = await health.claim_probe(key, "a" * 64, "b" * 64)
        assert probe and await health.complete_probe(probe, Capabilities(True, True, True, True))
        attempts = []
        with psycopg.connect(database["operator"]) as conn:
            for _ in range(6):
                uid = uuid4()
                attempts.append(uid)
                conn.execute(
                    """INSERT INTO app.model_usage(workspace_id,id,user_id,attempt_no,purpose,model_key,resolved_model_id,price_version,
                    reserved_micro_usd,cost_micro_usd,state,day_start,month_start,execution_state,failure_code,failure_retryable)
                    VALUES(%s,%s,%s,1,'generation',%s,'fixture',%s,0,0,'settled',current_date,date_trunc('month',current_date)::date,'definite_failure','provider_rate_limited',true)""",
                    (workspace, uid, user, key, "a" * 64),
                )
        await health.record_attempt(workspace, attempts[0], "a" * 64, "b" * 64)
        await health.record_attempt(workspace, attempts[0], "a" * 64, "b" * 64)
        with psycopg.connect(database["operator"]) as conn:
            assert (
                conn.execute(
                    "SELECT transient_failures FROM app.provider_health WHERE model_key=%s", (key,)
                ).fetchone()[0]
                == 1
            )
            # Old failures age out of the actual rolling window, without resetting current observations.
            conn.execute(
                "UPDATE app.provider_health SET failure_times=ARRAY[now()-interval '61 seconds'] WHERE model_key=%s",
                (key,),
            )
        for uid in attempts[1:5]:
            await health.record_attempt(workspace, uid, "a" * 64, "b" * 64)
        with psycopg.connect(database["operator"]) as conn:
            assert conn.execute(
                "SELECT state,transient_failures FROM app.provider_health WHERE model_key=%s",
                (key,),
            ).fetchone() == ("closed", 4)
        await asyncio.gather(
            health.record_attempt(workspace, attempts[5], "a" * 64, "b" * 64),
            health.record_attempt(workspace, attempts[5], "a" * 64, "b" * 64),
        )
        with psycopg.connect(database["operator"]) as conn:
            assert conn.execute(
                "SELECT state,transient_failures FROM app.provider_health WHERE model_key=%s",
                (key,),
            ).fetchone() == ("open", 5)
            conn.execute(
                "UPDATE app.provider_health SET open_until=now()-interval '1 second' WHERE model_key=%s",
                (key,),
            )
        reopened = await health.claim_probe(key, "a" * 64, "b" * 64)
        assert reopened and await health.complete_probe(
            reopened, Capabilities(True, True, True, True)
        )
        # A reported old config cannot change this endpoint's freshly reset breaker.
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.model_usage SET health_recorded=false WHERE workspace_id=%s AND id=%s",
                (workspace, attempts[5]),
            )
        await health.record_attempt(workspace, attempts[5], "c" * 64, "b" * 64)
        with psycopg.connect(database["operator"]) as conn:
            assert conn.execute(
                "SELECT state,transient_failures FROM app.provider_health WHERE model_key=%s",
                (key,),
            ).fetchone() == ("closed", 0)
    finally:
        await db.dispose()


async def test_operator_probe_budget_is_global_atomic_conservative_and_utc(database):
    from evidencedesk.errors import Problem

    db = engine(database["ed_dispatch"])
    health = ProviderHealth(db)
    seed, first, second = ["budget_" + uuid4().hex for _ in range(3)]
    try:
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                """INSERT INTO app.provider_health(model_key,state,window_start,probe_budget_day,probe_reserved_micro_usd)
                SELECT %s,'closed',now(),(now() AT TIME ZONE 'UTC')::date,999000-COALESCE(sum(probe_reserved_micro_usd),0)
                FROM app.provider_health WHERE probe_budget_day=(now() AT TIME ZONE 'UTC')::date""",
                (seed,),
            )
        outcomes = await asyncio.gather(
            health.claim_probe(first, "a" * 64, "b" * 64, reserved_micro_usd=1000),
            health.claim_probe(second, "a" * 64, "b" * 64, reserved_micro_usd=1000),
            return_exceptions=True,
        )
        success = [o for o in outcomes if not isinstance(o, Exception)]
        errors = [o for o in outcomes if isinstance(o, Problem)]
        assert len(success) == len(errors) == 1
        assert errors[0].code == "probe_budget_exceeded"
        assert await health.complete_probe(success[0], Capabilities(False, False, False, False))
        # Failed/cancelled/unknown calls retain the full quote; restart cannot reclaim it.
        with pytest.raises(Problem, match="probe_budget_exceeded"):
            await ProviderHealth(db).claim_probe(seed, "a" * 64, "c" * 64, reserved_micro_usd=1)
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.provider_health SET probe_budget_day=(now() AT TIME ZONE 'UTC')::date-1 WHERE model_key=ANY(%s)",
                ([seed, first, second],),
            )
        assert await health.claim_probe(seed, "a" * 64, "c" * 64, reserved_micro_usd=1)
    finally:
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "DELETE FROM app.provider_health WHERE model_key=ANY(%s)", ([seed, first, second],)
            )
        await db.dispose()

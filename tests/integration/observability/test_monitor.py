import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from evidencedesk.db.session import engine
from evidencedesk.observability.ledger_metrics import LedgerMetrics
from prometheus_client import CollectorRegistry, generate_latest

from tests.integration.tenancy.test_workspaces import headers
from tests.support import sign_in

pytestmark = pytest.mark.integration


def test_monitor_actual_command_with_only_monitor_credentials(database):
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("ED_", "LANGSMITH_", "LANGCHAIN_"))
    }
    env["ED_MONITOR_DATABASE_URL"] = database["ed_monitor"]
    result = subprocess.run(
        [str(Path(sys.executable).with_name("evidencedesk")), "metrics-worker", "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert database["ed_monitor"] not in result.stdout + result.stderr


def test_monitor_privileges_are_fixed_aggregates_only(database):
    with psycopg.connect(database["ed_monitor"], autocommit=True) as conn:
        assert conn.execute(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == (False, False)
        assert isinstance(conn.execute("SELECT app.operational_metrics()").fetchone()[0], list)
        for query in [
            "SELECT * FROM app.model_usage",
            "SELECT * FROM app.documents",
            "SELECT * FROM app.sessions",
            "SELECT * FROM lg_checkpoints.checkpoints",
            "SELECT app.sweep_expired_jobs()",
        ]:
            with pytest.raises(psycopg.Error):
                conn.execute(query)
    for name in ("ed_app", "ed_dispatch", "ed_identity"):
        with psycopg.connect(database[name], autocommit=True) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT app.operational_metrics()")


async def test_ledger_snapshot_is_unsampled_and_drops_stale_values(
    auth_client, database, monkeypatch
):
    client, state = auth_client
    session = sign_in(client, state)
    wid = client.post(
        "/v1/workspaces", json={"name": "Monitor fixture"}, headers=headers(session)
    ).json()["id"]
    db = engine(database["ed_monitor"])
    collector, registry = LedgerMetrics(), CollectorRegistry()
    registry.register(collector)
    try:
        assert await collector.refresh(db)
        baseline = {(s.name, s.labels): s.value for s in collector.values}
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                """INSERT INTO app.model_usage(workspace_id,id,user_id,attempt_no,purpose,model_key,resolved_model_id,price_version,reserved_micro_usd,cost_micro_usd,state,day_start,month_start,input_tokens,output_tokens,reasoning_output_tokens)
            VALUES(%s,%s,%s,1,'evaluation','CANARY-PRIVATE-MODEL','PRIVATE-SECRET-CANARY','fixture',75,75,'settled',current_date,current_date,100,20,10)""",
                (wid, uuid4(), session["user"]["id"]),
            )
        assert await collector.refresh(db)
        text = generate_latest(registry).decode()
        observed = {(s.name, s.labels): s.value for s in collector.values}
        cost_key = ("ed_model_cost_micro_usd", ("other", "false"))
        output_key = ("ed_model_tokens", ("other", "output"))
        assert observed[cost_key] == baseline.get(cost_key, 0) + 75
        assert observed[output_key] == baseline.get(output_key, 0) + 20  # Thinking is a subset.
        assert "SECRET-CANARY" not in text and "PRIVATE-MODEL" not in text and wid not in text
        collector.last_success -= 46
        stale = generate_latest(registry).decode()
        assert "ed_monitor_snapshot_success 0.0" in stale and "ed_model_tokens{" not in stale

        async def broken(_):
            raise RuntimeError("DB-SECRET-CANARY")

        monkeypatch.setattr("evidencedesk.observability.ledger_metrics.snapshot", broken)
        assert not await collector.refresh(db)
        assert "SECRET-CANARY" not in generate_latest(registry).decode()
    finally:
        await db.dispose()

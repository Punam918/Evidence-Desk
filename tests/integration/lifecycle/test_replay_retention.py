import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from evidencedesk.db import replay_retention_repository as repo
from evidencedesk.db.session import engine
from evidencedesk.lifecycle.replay import ReplayRetention

from tests.support import headers, sign_in

pytestmark = pytest.mark.integration


@pytest.fixture
async def replay_data(auth_client, database):
    client, state = auth_client
    session = sign_in(client, state)
    wid = client.post(
        "/v1/workspaces", json={"name": "Replay retention"}, headers=headers(session)
    ).json()["id"]
    base = f"/v1/workspaces/{wid}"
    kb = client.post(
        base + "/knowledge-bases", json={"name": "Sources"}, headers=headers(session)
    ).json()["id"]
    cid = client.post(
        base + "/conversations", json={"title": "Saved history"}, headers=headers(session)
    ).json()["id"]
    response = client.post(
        base + f"/conversations/{cid}/runs",
        json={
            "query": "Private query",
            "kb_ids": [kb],
            "model_key": "local_standard",
            "intent": "ask",
            "expected_conversation_version": 1,
        },
        headers=headers(session),
    )
    assert response.status_code == 202, response.text
    run, usage = UUID(response.json()["id"]), uuid4()
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.runs SET status='completed',created_at=now()-interval '25 hours',completed_at=now()-interval '24 hours' WHERE id=%s",
            (run,),
        )
        conn.execute("UPDATE app.jobs SET status='succeeded' WHERE run_id=%s", (run,))
        conn.execute(
            "UPDATE app.idempotency_keys SET expires_at=now()-interval '1 hour' WHERE workspace_id=%s",
            (wid,),
        )
        conn.execute(
            """INSERT INTO app.model_usage(workspace_id,id,run_id,conversation_id,user_id,attempt_no,purpose,model_key,resolved_model_id,price_version,
              reserved_micro_usd,cost_micro_usd,input_tokens,output_tokens,state,execution_state,response,day_start,month_start,created_at)
            VALUES(%s,%s,%s,%s,%s,1,'generation','local_standard','fixture','fixture',10,7,8,9,'settled','response_stored',
              '{"private":"Provider response body"}',current_date,current_date,now()-interval '25 hours')""",
            (wid, usage, run, cid, session["user"]["id"]),
        )
    db = engine(database["ed_app"], pool_size=2)
    try:
        yield ReplayRetention(db), UUID(wid), run, usage, client, base, cid
    finally:
        await db.dispose()


def read_counts(database, wid):
    with psycopg.connect(database["operator"]) as conn:
        return tuple(
            conn.execute(
                f"SELECT count(*) FROM app.{table} WHERE workspace_id=%s", (wid,)
            ).fetchone()[0]
            for table in ("idempotency_keys", "run_events", "messages", "model_usage")
        )


async def test_expiry_preserves_saved_history_accounting_and_event_api_contract(
    replay_data, database
):
    retention, wid, run, usage, client, base, cid = replay_data
    before = read_counts(database, wid)
    page = await retention.prune_workspace(wid)
    assert (page.idempotency_keys, page.events, page.responses) == (before[0], before[1], 1)
    assert read_counts(database, wid) == (0, 0, before[2], before[3])
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT response,state,execution_state,cost_micro_usd,input_tokens,output_tokens,run_id FROM app.model_usage WHERE id=%s",
            (usage,),
        ).fetchone() == (None, "settled", "response_stored", 7, 8, 9, run)
    assert client.get(base + f"/conversations/{cid}").status_code == 200
    expired = client.get(base + f"/runs/{run}/events")
    assert expired.status_code == 410 and expired.json()["code"] == "events_expired"
    again = await retention.prune_workspace(wid)
    assert (again.idempotency_keys, again.events, again.responses, again.may_have_more) == (
        0,
        0,
        0,
        False,
    )


@pytest.mark.parametrize("guard", ["active", "queued_job", "reserved", "fresh"])
async def test_needed_provider_responses_survive(replay_data, database, guard):
    retention, wid, run, usage, *_ = replay_data
    with psycopg.connect(database["operator"]) as conn:
        if guard == "active":
            conn.execute("UPDATE app.runs SET status='running' WHERE id=%s", (run,))
        elif guard == "queued_job":
            conn.execute("UPDATE app.jobs SET status='retry_wait' WHERE run_id=%s", (run,))
        elif guard == "reserved":
            conn.execute("UPDATE app.model_usage SET state='reserved' WHERE id=%s", (usage,))
        else:
            conn.execute("UPDATE app.model_usage SET created_at=now() WHERE id=%s", (usage,))
    page = await retention.prune_workspace(wid)
    assert page.responses == 0
    if guard == "active":
        assert page.events == 0
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT response IS NOT NULL FROM app.model_usage WHERE id=%s", (usage,)
        ).fetchone() == (True,)


async def test_workspace_scope_bounded_batches_and_locked_rows(replay_data, database):
    retention, wid, _, usage, *_ = replay_data
    before = read_counts(database, wid)
    foreign = await retention.prune_workspace(uuid4())
    assert (foreign.idempotency_keys, foreign.events, foreign.responses) == (0, 0, 0)
    assert read_counts(database, wid) == before
    # A live writer holds only this row. Sweep skips it without blocking other work.
    with psycopg.connect(database["operator"]) as conn:
        conn.execute("SELECT id FROM app.model_usage WHERE id=%s FOR UPDATE", (usage,))
        page = await retention.prune_workspace(wid, limit=1)
        assert page.idempotency_keys == 1 and page.events == 1 and page.responses == 0
        assert page.may_have_more
    assert (await retention.prune_workspace(wid, limit=1)).responses == 1
    for _ in range(before[0] + before[1]):
        page = await retention.prune_workspace(wid, limit=1)
        if not page.may_have_more:
            break
    assert read_counts(database, wid)[:2] == (0, 0)


async def test_fresh_keys_and_events_survive_and_fault_rolls_back_whole_batch(
    replay_data, database, monkeypatch
):
    retention, wid, run, usage, *_ = replay_data
    before = read_counts(database, wid)
    original = repo.prune

    async def crash(*args):
        await original(*args)
        raise RuntimeError("Synthetic transaction failure")

    with monkeypatch.context() as patch:
        patch.setattr(repo, "prune", crash)
        with pytest.raises(RuntimeError):
            await retention.prune_workspace(wid)
    assert read_counts(database, wid) == before
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT response IS NOT NULL FROM app.model_usage WHERE id=%s", (usage,)
        ).fetchone() == (True,)
        conn.execute("UPDATE app.runs SET created_at=now() WHERE id=%s", (run,))
        conn.execute(
            "UPDATE app.idempotency_keys SET expires_at=now()+interval '1 hour' WHERE workspace_id=%s",
            (wid,),
        )
    page = await retention.prune_workspace(wid)
    assert page.idempotency_keys == page.events == 0
    assert page.responses == 1
    assert read_counts(database, wid) == before


async def test_actual_replay_command_needs_only_application_database(replay_data, database):
    _, wid, *_ = replay_data
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("ED_", "LANGSMITH_", "LANGCHAIN_"))
    }
    env["ED_DATABASE_URL"] = database["ed_app"]
    result = subprocess.run(
        [
            str(Path(sys.executable).with_name("evidencedesk")),
            "replay-prune",
            "--workspace",
            str(wid),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["responses"] == 1
    assert "Provider response body" not in result.stdout + result.stderr

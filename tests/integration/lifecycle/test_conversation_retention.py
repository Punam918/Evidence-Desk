import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from evidencedesk.db import conversation_retention_repository as repo
from evidencedesk.lifecycle.conversation_retention import ConversationRetention

from tests.integration.lifecycle.test_conversation_purge import finish
from tests.integration.lifecycle.test_conversation_purge import purge_data as purge_data
from tests.integration.lifecycle.test_replay_retention import replay_data as replay_data
from tests.support import headers

pytestmark = pytest.mark.integration


async def test_expiry_queues_existing_purge_and_removes_native_checkpoints(purge_data):
    h = purge_data
    retention = ConversationRetention(h.db)
    assert (await retention.expire_workspace(h.wid)).expired == 0
    with psycopg.connect(h.database["operator"]) as conn:
        conn.execute(
            "UPDATE app.conversations SET created_at=now()-interval '91 days',updated_at=now() WHERE id=%s",
            (h.cid,),
        )
    page = await retention.expire_workspace(h.wid)
    assert page.expired == 1 and not page.may_have_more
    assert h.client.get(h.base + f"/conversations/{h.cid}").status_code == 404
    with psycopg.connect(h.database["operator"]) as conn:
        h.job, actor = conn.execute(
            "SELECT id,requested_by FROM app.jobs WHERE workspace_id=%s AND dedupe_key=%s",
            (h.wid, f"purge:conversation:{h.cid}"),
        ).fetchone()
        assert actor == h.actor
        assert conn.execute(
            "SELECT count(*) FROM app.deletion_tombstones WHERE workspace_id=%s AND entity_id=%s",
            (h.wid, h.cid),
        ).fetchone() == (1,)
    assert (await retention.expire_workspace(h.wid)).expired == 0
    await finish(h)
    with psycopg.connect(h.database["operator"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.conversations WHERE id=%s", (h.cid,)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM app.messages WHERE conversation_id=%s", (h.cid,)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT run_id,conversation_id,cost_micro_usd FROM app.model_usage WHERE id=%s",
            (h.usage,),
        ).fetchone() == (None, None, 7)
        assert conn.execute(
            "SELECT count(*) FROM app.deletion_tombstones WHERE workspace_id=%s AND entity_type='workflow_thread' AND purged_at IS NOT NULL",
            (h.wid,),
        ).fetchone() == (2,)


async def test_expiry_is_scoped_paged_and_works_after_creator_membership_removal(
    replay_data, database
):
    existing, wid, _, _, client, base, cid = replay_data
    actor = UUID(client.get("/v1/me").json()["user"]["id"])
    retention = ConversationRetention(existing.db)
    session = client.get("/v1/me").json()
    fresh = client.post(
        base + "/conversations", json={"title": "Fresh"}, headers=headers(session)
    ).json()["id"]
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.conversations SET created_at=now()-interval '91 days' WHERE id=%s", (cid,)
        )
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO app.conversations(workspace_id,id,user_id,title,created_at) VALUES(%s,%s,%s,'Old private chat',now()-interval '91 days')",
                [(wid, uuid4(), actor) for _ in range(26)],
            )
        conn.execute(
            "DELETE FROM app.memberships WHERE workspace_id=%s AND user_id=%s", (wid, actor)
        )
    assert (await retention.expire_workspace(uuid4())).expired == 0
    first = await retention.expire_workspace(wid)
    assert first.expired == 25 and first.may_have_more
    second = await retention.expire_workspace(wid)
    assert second.expired == 2 and not second.may_have_more
    assert (await retention.expire_workspace(wid)).expired == 0
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT id FROM app.conversations WHERE workspace_id=%s AND deleted_at IS NULL", (wid,)
        ).fetchall() == [(UUID(fresh),)]
        assert conn.execute(
            "SELECT count(*) FROM app.jobs WHERE workspace_id=%s AND kind='purge' AND requested_by=%s",
            (wid, actor),
        ).fetchone() == (27,)


async def test_failure_rolls_back_tombstone_job_and_cancellation(
    replay_data, database, monkeypatch
):
    existing, wid, run, _, client, base, cid = replay_data
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.conversations SET created_at=now()-interval '91 days' WHERE id=%s", (cid,)
        )
        conn.execute("UPDATE app.runs SET status='running' WHERE id=%s", (run,))
    original = repo.expire

    async def fail(*args):
        await original(*args)
        raise RuntimeError("Synthetic commit failure")

    retention = ConversationRetention(existing.db)
    with monkeypatch.context() as patch:
        patch.setattr(repo, "expire", fail)
        with pytest.raises(RuntimeError):
            await retention.expire_workspace(wid)
    assert client.get(base + f"/conversations/{cid}").status_code == 200
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT cancel_requested FROM app.runs WHERE id=%s", (run,)
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT count(*) FROM app.deletion_tombstones WHERE workspace_id=%s", (wid,)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM app.jobs WHERE workspace_id=%s AND kind='purge'", (wid,)
        ).fetchone() == (0,)
    assert (await retention.expire_workspace(wid)).expired == 1
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT cancel_requested FROM app.runs WHERE id=%s", (run,)
        ).fetchone() == (True,)


async def test_actual_expiry_cli_only_needs_application_database(replay_data, database):
    _, wid, _, _, _, _, cid = replay_data
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.conversations SET created_at=now()-interval '91 days' WHERE id=%s", (cid,)
        )
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("ED_", "LANGCHAIN_", "LANGSMITH_"))
    }
    env["ED_DATABASE_URL"] = database["ed_app"]
    result = subprocess.run(
        [
            str(Path(sys.executable).with_name("evidencedesk")),
            "conversation-expire",
            "--workspace",
            str(wid),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "event": "conversation_expiry",
        "expired": 1,
        "may_have_more": False,
    }
    assert "Private query" not in result.stdout + result.stderr

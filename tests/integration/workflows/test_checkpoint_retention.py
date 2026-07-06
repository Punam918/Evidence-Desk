import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from evidencedesk.db import checkpoint_retention_repository as repo
from evidencedesk.db.session import AuthContext, engine, tenant_transaction
from evidencedesk.db.workflow_repository import require_checkpoint_live
from evidencedesk.errors import Problem
from evidencedesk.lifecycle.checkpoints import CheckpointRetention
from evidencedesk.workflows.checkpoint_codec import CheckpointCodec
from evidencedesk.workflows.observed_saver import ObservedSaver
from evidencedesk.workflows.thread_gate import thread_gate
from langgraph.checkpoint.base import empty_checkpoint
from psycopg.rows import dict_row

from tests.support import headers, sign_in

pytestmark = pytest.mark.integration


async def write_checkpoints(database, thread):
    async with await psycopg.AsyncConnection.connect(
        database["ed_checkpoint"], autocommit=True, row_factory=dict_row
    ) as conn:
        saver = ObservedSaver(conn, serde=CheckpointCodec())
        for namespace in ("", "diagnostic"):
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = {"reference": {"ids": [str(thread)]}}
            checkpoint["channel_versions"] = {"reference": "1"}
            config = await saver.aput(
                {"configurable": {"thread_id": str(thread), "checkpoint_ns": namespace}},
                checkpoint,
                {"source": "input", "step": -1, "parents": {}},
                {"reference": "1"},
            )
            await saver.aput_writes(config, [("reference", str(thread))], str(uuid4()))


def counts(database, thread):
    with psycopg.connect(database["operator"]) as conn:
        return tuple(
            conn.execute(
                f"SELECT count(*) FROM lg_checkpoints.{table} WHERE thread_id=%s", (str(thread),)
            ).fetchone()[0]
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
        )


@pytest.fixture
async def retained(auth_client, database):
    client, state = auth_client
    session = sign_in(client, state)
    wid = client.post(
        "/v1/workspaces", json={"name": "Retention"}, headers=headers(session)
    ).json()["id"]
    base = f"/v1/workspaces/{wid}"
    kb = client.post(
        base + "/knowledge-bases", json={"name": "Sources"}, headers=headers(session)
    ).json()["id"]
    created = []

    async def create():
        conversation = client.post(
            base + "/conversations",
            json={"title": "Application history remains"},
            headers=headers(session),
        ).json()["id"]
        response = client.post(
            base + f"/conversations/{conversation}/runs",
            json={
                "query": "Private content",
                "kb_ids": [kb],
                "model_key": "local_standard",
                "intent": "ask",
                "expected_conversation_version": 1,
            },
            headers=headers(session),
        )
        assert response.status_code == 202, response.text
        run = UUID(response.json()["id"])
        with psycopg.connect(database["operator"]) as conn:
            thread = conn.execute(
                "SELECT id FROM app.workflow_threads WHERE workspace_id=%s AND run_id=%s",
                (wid, run),
            ).fetchone()[0]
            conn.execute(
                "UPDATE app.runs SET status='completed',completed_at=now() WHERE id=%s", (run,)
            )
            conn.execute(
                "UPDATE app.jobs SET status='succeeded',lease_until=NULL WHERE run_id=%s", (run,)
            )
            conn.execute(
                "UPDATE app.workflow_threads SET status='completed',expires_at=now()-interval '1 hour' WHERE id=%s",
                (thread,),
            )
        await write_checkpoints(database, thread)
        created.append((run, thread))
        return run, thread

    db = engine(database["ed_app"])
    ctx = AuthContext(UUID(session["user"]["id"]), UUID(wid), "owner", 1, uuid4())
    try:
        run, thread = await create()
        yield CheckpointRetention(db, database["ed_checkpoint"]), ctx, run, thread, create
    finally:
        await db.dispose()


async def test_retention_removes_all_namespaces_blobs_writes_and_preserves_application_rows(
    retained, database
):
    retention, ctx, run, thread, _ = retained
    assert all(n >= 2 for n in counts(database, thread))
    page = await retention.prune_workspace(ctx.workspace_id)
    assert (page.checked, page.purged, page.busy, page.next_thread) == (1, 1, 0, None)
    assert counts(database, thread) == (0, 0, 0)
    assert (await retention.prune_workspace(ctx.workspace_id)).checked == 0
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT checkpoint_purged_at IS NOT NULL FROM app.workflow_threads WHERE id=%s",
            (thread,),
        ).fetchone() == (True,)
        assert conn.execute("SELECT status FROM app.runs WHERE id=%s", (run,)).fetchone() == (
            "completed",
        )
        assert conn.execute(
            "SELECT count(*) FROM app.messages WHERE workspace_id=%s", (ctx.workspace_id,)
        ).fetchone() == (2,)
        assert (
            conn.execute("SELECT count(*) FROM app.run_events WHERE run_id=%s", (run,)).fetchone()[
                0
            ]
            > 0
        )
    async with tenant_transaction(retention.db, ctx) as conn:
        with pytest.raises(Problem, match="expired"):
            await require_checkpoint_live(conn, ctx, thread)


@pytest.mark.parametrize(
    "guard", ["future", "run_active", "thread_waiting", "job_queued", "usage_reserved"]
)
async def test_retention_never_discards_needed_or_unsettled_progress(retained, database, guard):
    retention, ctx, run, thread, _ = retained
    with psycopg.connect(database["operator"]) as conn:
        if guard == "future":
            conn.execute(
                "UPDATE app.workflow_threads SET expires_at=now()+interval '1 day' WHERE id=%s",
                (thread,),
            )
        elif guard == "run_active":
            conn.execute("UPDATE app.runs SET status='running' WHERE id=%s", (run,))
        elif guard == "thread_waiting":
            conn.execute("UPDATE app.workflow_threads SET status='waiting' WHERE id=%s", (thread,))
        elif guard == "job_queued":
            conn.execute("UPDATE app.jobs SET status='queued' WHERE run_id=%s", (run,))
        else:
            conn.execute(
                """INSERT INTO app.model_usage(workspace_id,id,run_id,user_id,attempt_no,purpose,model_key,resolved_model_id,price_version,reserved_micro_usd,state,day_start,month_start)
                VALUES(%s,%s,%s,%s,1,'generation','local_standard','fixture','fixture',10,'reserved',current_date,current_date)""",
                (ctx.workspace_id, uuid4(), run, ctx.user_id),
            )
    assert (await retention.prune_workspace(ctx.workspace_id)).checked == 0
    assert all(counts(database, thread))


async def test_retention_respects_workspace_gate_and_keyset_pages(retained, database):
    retention, ctx, _, thread, create = retained
    assert (await retention.prune_workspace(uuid4())).checked == 0
    async with thread_gate(database["ed_checkpoint"], thread):
        page = await retention.prune_workspace(ctx.workspace_id)
        assert page.busy == 1 and page.purged == 0
        assert all(counts(database, thread))
    _, second = await create()
    first_page = await retention.prune_workspace(ctx.workspace_id, limit=1)
    assert first_page.purged == 1 and first_page.next_thread == min(thread, second)
    second_page = await retention.prune_workspace(ctx.workspace_id, first_page.next_thread, limit=1)
    assert second_page.purged == 1 and second_page.next_thread == max(thread, second)
    assert (
        await retention.prune_workspace(ctx.workspace_id, second_page.next_thread, limit=1)
    ).checked == 0
    assert counts(database, thread) == counts(database, second) == (0, 0, 0)


@pytest.mark.parametrize("failure", ["saver_before", "saver_after", "receipt"])
async def test_retention_partial_failure_is_idempotently_retried(
    retained, database, monkeypatch, failure
):
    retention, ctx, _, thread, _ = retained
    original = ObservedSaver.adelete_thread

    async def broken_saver(self, thread_id):
        if failure == "saver_after":
            await original(self, thread_id)
        raise RuntimeError("Synthetic saver outage")

    async def broken_receipt(*args):
        raise RuntimeError("Synthetic receipt commit failure")

    with monkeypatch.context() as patch:
        if failure == "receipt":
            patch.setattr(repo, "record_deleted", broken_receipt)
        else:
            patch.setattr(ObservedSaver, "adelete_thread", broken_saver)
        with pytest.raises(RuntimeError):
            await retention.prune_workspace(ctx.workspace_id)
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT checkpoint_purged_at FROM app.workflow_threads WHERE id=%s", (thread,)
        ).fetchone() == (None,)
    assert (await retention.prune_workspace(ctx.workspace_id)).purged == 1
    assert counts(database, thread) == (0, 0, 0)


async def test_retention_rechecks_eligibility_after_gate_and_reconciles_late_writes(
    retained, database, monkeypatch
):
    retention, ctx, run, thread, _ = retained
    original = repo.due

    async def changed(*args):
        rows = await original(*args)
        with psycopg.connect(database["operator"]) as conn:
            conn.execute("UPDATE app.jobs SET status='queued' WHERE run_id=%s", (run,))
        return rows

    with monkeypatch.context() as patch:
        patch.setattr(repo, "due", changed)
        assert (await retention.prune_workspace(ctx.workspace_id)).skipped == 1
    assert all(counts(database, thread))
    with psycopg.connect(database["operator"]) as conn:
        conn.execute("UPDATE app.jobs SET status='succeeded' WHERE run_id=%s", (run,))
    assert (await retention.prune_workspace(ctx.workspace_id)).purged == 1
    await write_checkpoints(
        database, thread
    )  # Simulate a delayed already-issued saver write after gate loss.
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.workflow_threads SET checkpoint_purged_at=now()-interval '2 hours' WHERE id=%s",
            (thread,),
        )
    assert (await retention.prune_workspace(ctx.workspace_id)).purged == 1
    assert counts(database, thread) == (0, 0, 0)


async def test_actual_checkpoint_retention_command_requires_only_scoped_db_credentials(
    retained, database
):
    retention, ctx, _, thread, _ = retained
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("ED_", "LANGSMITH_", "LANGCHAIN_"))
    }
    env.update(
        ED_DATABASE_URL=database["ed_app"], ED_CHECKPOINT_DATABASE_URL=database["ed_checkpoint"]
    )
    result = subprocess.run(
        [
            str(Path(sys.executable).with_name("evidencedesk")),
            "checkpoint-prune",
            "--workspace",
            str(ctx.workspace_id),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["purged"] == 1
    assert "Private content" not in result.stdout + result.stderr
    assert counts(database, thread) == (0, 0, 0)

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from evidencedesk.db import conversation_purge_repository as repo
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext, engine, tenant_transaction
from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.lifecycle.conversations import ConversationPurger
from evidencedesk.workflows.observed_saver import ObservedSaver
from evidencedesk.workflows.thread_gate import thread_gate

from tests.integration.lifecycle.test_replay_retention import replay_data as replay_data
from tests.integration.workflows.test_checkpoint_retention import counts, write_checkpoints
from tests.support import headers

pytestmark = pytest.mark.integration


@pytest.fixture
async def purge_data(replay_data, database):
    retention, wid, run, usage, client, base, cid = replay_data
    session = client.get("/v1/me").json()
    actor = UUID(session["user"]["id"])
    with psycopg.connect(database["operator"]) as conn:
        thread = conn.execute(
            "SELECT id FROM app.workflow_threads WHERE run_id=%s", (run,)
        ).fetchone()[0]
        message = conn.execute(
            "SELECT assistant_message_id FROM app.runs WHERE id=%s", (run,)
        ).fetchone()[0]
        tool, brief_thread = uuid4(), uuid4()
        conn.execute(
            """INSERT INTO app.tool_executions(workspace_id,id,run_id,user_id,tool_name,tool_version,arguments,arguments_sha256,payload_sha256,status)
          VALUES(%s,%s,%s,%s,'save_brief','1','{"title":"Private brief"}',%s,%s,'succeeded')""",
            (wid, tool, run, actor, "a" * 64, "b" * 64),
        )
        conn.execute(
            """INSERT INTO app.briefs(workspace_id,id,user_id,execution_id,answer_message_id,title,answer,citations)
          VALUES(%s,%s,%s,%s,%s,'Private brief','{"private":"Saved answer"}','[]')""",
            (wid, uuid4(), actor, tool, message),
        )
        conn.execute(
            """INSERT INTO app.workflow_threads(workspace_id,id,user_id,run_id,tool_execution_id,graph_name,graph_version,status,expires_at)
          VALUES(%s,%s,%s,%s,%s,'brief_v1','1','completed',now()+interval '1 day')""",
            (wid, brief_thread, actor, run, tool),
        )
        conn.execute(
            """INSERT INTO app.feedback(workspace_id,message_id,user_id,vote,reason)
          VALUES(%s,%s,%s,1,'Private feedback')""",
            (wid, message, actor),
        )
    for uid in (thread, brief_thread):
        await write_checkpoints(database, uid)
    dispatch = engine(database["ed_dispatch"])
    jobs = JobRepository(dispatch)
    h = SimpleNamespace(
        db=retention.db,
        jobs=jobs,
        purger=ConversationPurger(retention.db, jobs, database["ed_checkpoint"]),
        wid=wid,
        run=run,
        usage=usage,
        client=client,
        base=base,
        cid=UUID(cid),
        actor=actor,
        threads=(thread, brief_thread),
        database=database,
        session=session,
        tool=tool,
    )
    try:
        yield h
    finally:
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.jobs SET status='cancelled',lease_until=NULL WHERE workspace_id=%s AND status IN ('queued','running','retry_wait')",
                (wid,),
            )
        await dispatch.dispose()


def delete(h):
    response = h.client.delete(h.base + f"/conversations/{h.cid}", headers=headers(h.session))
    assert response.status_code == 202, response.text
    h.job = UUID(response.json()["job_id"])
    assert h.client.get(h.base + f"/conversations/{h.cid}").status_code == 404


async def step(h):
    with psycopg.connect(h.database["operator"]) as conn:
        conn.execute(
            "UPDATE app.jobs SET available_at=now()-interval '1 second' WHERE id=%s", (h.job,)
        )
    lease = await h.jobs.claim_conversation_purge("purge-test")
    assert lease and lease.job_id == h.job
    await h.purger.run(lease)
    return lease


async def finish(h):
    for _ in range(15):
        await step(h)
        with psycopg.connect(h.database["operator"]) as conn:
            if conn.execute("SELECT status FROM app.jobs WHERE id=%s", (h.job,)).fetchone() == (
                "succeeded",
            ):
                return
    raise AssertionError("Purge did not converge")


async def test_purge_erases_all_private_rows_preserves_accounting_and_reconciles_late_saver_write(
    purge_data,
):
    h = purge_data
    delete(h)
    await finish(h)
    with psycopg.connect(h.database["operator"]) as conn:
        for table in (
            "conversations",
            "messages",
            "runs",
            "run_events",
            "workflow_threads",
            "tool_executions",
            "briefs",
            "feedback",
        ):
            assert conn.execute(
                f"SELECT count(*) FROM app.{table} WHERE workspace_id=%s", (h.wid,)
            ).fetchone() == (0,), table
        assert conn.execute(
            "SELECT count(*) FROM app.knowledge_bases WHERE workspace_id=%s", (h.wid,)
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT run_id,conversation_id,response,request_sha256,cost_micro_usd,input_tokens,output_tokens FROM app.model_usage WHERE id=%s",
            (h.usage,),
        ).fetchone() == (None, None, None, None, 7, 8, 9)
        assert conn.execute(
            "SELECT count(*) FROM app.deletion_tombstones WHERE workspace_id=%s AND purged_at IS NOT NULL",
            (h.wid,),
        ).fetchone() == (3,)
        assert conn.execute(
            "SELECT count(*) FROM app.idempotency_keys WHERE workspace_id=%s AND response_body::text LIKE %s",
            (h.wid, "%Saved history%"),
        ).fetchone() == (0,)
    for thread in h.threads:
        assert counts(h.database, thread) == (0, 0, 0)
    await write_checkpoints(h.database, h.threads[0])
    with psycopg.connect(h.database["operator"]) as conn:
        conn.execute(
            "UPDATE app.deletion_tombstones SET purged_at=now()-interval '2 hours' WHERE workspace_id=%s AND entity_type='workflow_thread'",
            (h.wid,),
        )
    result = await h.purger.checkpoints.prune_workspace(h.wid)
    assert result.purged == 2 and counts(h.database, h.threads[0]) == (0, 0, 0)


@pytest.mark.parametrize(
    "execution,expected,state",
    [("not_issued", 0, "settled"), ("issued", 10, "estimated"), ("ambiguous", 10, "estimated")],
)
async def test_purge_invalidates_active_worker_and_settles_reserved_usage_once(
    purge_data, execution, expected, state
):
    h = purge_data
    with psycopg.connect(h.database["operator"]) as conn:
        conn.execute("UPDATE app.runs SET status='running',created_at=now() WHERE id=%s", (h.run,))
        conn.execute("UPDATE app.jobs SET status='queued' WHERE run_id=%s", (h.run,))
        conn.execute(
            "UPDATE app.model_usage SET state='reserved',execution_state=%s,cost_micro_usd=NULL,response=NULL,month_start=date_trunc('month',current_date)::date WHERE id=%s",
            (execution, h.usage),
        )
        conn.execute(
            "UPDATE app.budget_windows SET reserved_micro_usd=10,spent_micro_usd=0 WHERE workspace_id=%s",
            (h.wid,),
        )
    old = await h.jobs.claim("chat", "old-chat")
    assert old and old.run_id == h.run
    delete(h)
    await step(h)  # Cached responses first.
    async with thread_gate(h.database["ed_checkpoint"], h.threads[0]):
        await step(h)
        ctx = AuthContext(h.actor, h.wid, "owner", 1, uuid4())
        async with tenant_transaction(h.db, ctx) as conn:
            with pytest.raises(LeaseLost):
                await h.jobs.fence(conn, old)
        with psycopg.connect(h.database["operator"]) as conn:
            assert conn.execute(
                "SELECT cost_micro_usd,state FROM app.model_usage WHERE id=%s", (h.usage,)
            ).fetchone() == (expected, state)
            assert conn.execute(
                "SELECT count(*) FROM app.runs WHERE id=%s", (h.run,)
            ).fetchone() == (1,)
    await finish(h)
    with psycopg.connect(h.database["operator"]) as conn:
        assert conn.execute(
            "SELECT reserved_micro_usd,spent_micro_usd FROM app.budget_windows WHERE workspace_id=%s",
            (h.wid,),
        ).fetchall() == [(0, expected), (0, expected)]


@pytest.mark.parametrize(
    "failure", ["before_saver", "after_saver", "receipt", "before_run_delete", "after_run_delete"]
)
async def test_partial_purge_failures_retry_without_losing_thread_identity(
    purge_data, monkeypatch, failure
):
    h = purge_data
    delete(h)
    await step(h)
    original_saver = ObservedSaver.adelete_thread
    original_remove = repo.remove_run

    async def broken_saver(self, thread):
        if failure == "after_saver":
            await original_saver(self, thread)
        raise RuntimeError("Private upstream failure")

    async def broken_remove(*args):
        if failure == "after_run_delete":
            await original_remove(*args)
        raise RuntimeError("Private SQL failure")

    async def broken_receipt(*args):
        raise RuntimeError("Private receipt failure")

    from evidencedesk.db import checkpoint_retention_repository

    with monkeypatch.context() as patch:
        if failure in ("before_saver", "after_saver"):
            patch.setattr(ObservedSaver, "adelete_thread", broken_saver)
        elif failure == "receipt":
            patch.setattr(checkpoint_retention_repository, "record_deleted", broken_receipt)
        else:
            patch.setattr(repo, "remove_run", broken_remove)
        with pytest.raises(Problem, match="interrupted"):
            await step(h)
    with psycopg.connect(h.database["operator"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.deletion_tombstones WHERE workspace_id=%s AND entity_type='workflow_thread'",
            (h.wid,),
        ).fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM app.runs WHERE id=%s", (h.run,)).fetchone() == (
            1,
        )
        conn.execute(
            "UPDATE app.jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (h.job,)
        )
    await h.jobs.sweep()
    await finish(h)


@pytest.mark.parametrize(
    "change", ["live", "no_tombstone", "foreign_actor", "foreign_target", "stale_lease"]
)
async def test_purge_requires_tombstone_creator_and_current_lease(purge_data, change):
    h = purge_data
    delete(h)
    lease = await h.jobs.claim_conversation_purge("authorization-test")
    with psycopg.connect(h.database["operator"]) as conn:
        if change == "live":
            conn.execute("UPDATE app.conversations SET deleted_at=NULL WHERE id=%s", (h.cid,))
        elif change == "no_tombstone":
            conn.execute("DELETE FROM app.deletion_tombstones WHERE workspace_id=%s", (h.wid,))
        elif change == "foreign_actor":
            lease = replace(lease, actor_id=uuid4())
        elif change == "foreign_target":
            conn.execute(
                "UPDATE app.jobs SET payload=jsonb_build_object('entity_type','conversation','entity_id',%s::text) WHERE id=%s",
                (str(uuid4()), h.job),
            )
        else:
            conn.execute("UPDATE app.jobs SET lease_token=lease_token+1 WHERE id=%s", (h.job,))
    with pytest.raises((Problem, LeaseLost)):
        await h.purger.run(lease)
    assert all(counts(h.database, h.threads[0]))
    with psycopg.connect(h.database["operator"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.messages WHERE workspace_id=%s", (h.wid,)
        ).fetchone() == (2,)


async def test_actual_cli_and_dispatch_do_not_consume_source_purge_jobs(purge_data):
    h = purge_data
    delete(h)
    source_job = uuid4()
    with psycopg.connect(h.database["operator"]) as conn:
        conn.execute(
            """INSERT INTO app.jobs(workspace_id,id,requested_by,kind,status,payload,dedupe_key,created_at)
          VALUES(%s,%s,%s,'purge','queued','{"entity_type":"document"}',%s,now()-interval '1 day')""",
            (h.wid, source_job, h.actor, str(source_job)),
        )
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("ED_", "LANGCHAIN_", "LANGSMITH_"))
    }
    env.update(
        ED_DATABASE_URL=h.database["ed_app"],
        ED_DISPATCH_DATABASE_URL=h.database["ed_dispatch"],
        ED_CHECKPOINT_DATABASE_URL=h.database["ed_checkpoint"],
    )
    for _ in range(5):
        result = subprocess.run(
            [
                str(Path(sys.executable).with_name("evidencedesk")),
                "conversation-purge-worker",
                "--once",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Private" not in result.stdout + result.stderr
        with psycopg.connect(h.database["operator"]) as conn:
            conn.execute("UPDATE app.jobs SET available_at=now() WHERE id=%s", (h.job,))
            if conn.execute("SELECT status FROM app.jobs WHERE id=%s", (h.job,)).fetchone() == (
                "succeeded",
            ):
                break
    with psycopg.connect(h.database["operator"]) as conn:
        assert conn.execute(
            "SELECT status,attempts FROM app.jobs WHERE id=%s", (source_job,)
        ).fetchone() == ("queued", 0)
        assert conn.execute("SELECT status FROM app.jobs WHERE id=%s", (h.job,)).fetchone() == (
            "succeeded",
        )


async def test_pages_bound_replay_and_thread_work_and_survive_actor_departure(purge_data):
    h = purge_data
    other = h.client.post(
        h.base + "/conversations",
        json={"title": "Keep this conversation"},
        headers=headers(h.session),
    ).json()["id"]
    with psycopg.connect(h.database["operator"]) as conn:
        for i in range(26):
            tool, thread = uuid4(), uuid4()
            conn.execute(
                """INSERT INTO app.tool_executions(workspace_id,id,run_id,user_id,tool_name,tool_version,arguments,arguments_sha256,payload_sha256,status)
              VALUES(%s,%s,%s,%s,'save_brief','1','{}',%s,%s,'proposed')""",
                (h.wid, tool, h.run, h.actor, f"{i:064x}", "c" * 64),
            )
            conn.execute(
                """INSERT INTO app.workflow_threads(workspace_id,id,user_id,run_id,tool_execution_id,graph_name,graph_version,status,expires_at)
              VALUES(%s,%s,%s,%s,%s,'brief_v1','1','waiting',now()+interval '1 day')""",
                (h.wid, thread, h.actor, h.run, tool),
            )
        for i in range(105):
            conn.execute(
                """INSERT INTO app.idempotency_keys(workspace_id,user_id,method_path,key_hash,request_sha256,response_status,response_body,expires_at)
              VALUES(%s,%s,'POST /conversations',%s,%s,201,jsonb_build_object('id',%s::text,'title','Private cached title'),now()+interval '1 day')""",
                (h.wid, h.actor, f"cache-{i}", "a" * 64, str(h.cid)),
            )
    delete(h)
    with psycopg.connect(h.database["operator"]) as conn:
        conn.execute(
            "DELETE FROM app.memberships WHERE workspace_id=%s AND user_id=%s", (h.wid, h.actor)
        )
        before = conn.execute(
            "SELECT count(*) FROM app.idempotency_keys WHERE workspace_id=%s", (h.wid,)
        ).fetchone()[0]
    await step(h)
    with psycopg.connect(h.database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM app.idempotency_keys WHERE workspace_id=%s", (h.wid,)
            ).fetchone()[0]
            == before - 100
        )
    await step(h)  # Remaining matching cache rows.
    await step(h)  # Only25 of28 threads may be acknowledged on one page.
    with psycopg.connect(h.database["operator"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.deletion_tombstones WHERE workspace_id=%s AND entity_type='workflow_thread' AND purged_at IS NOT NULL",
            (h.wid,),
        ).fetchone() == (25,)
        assert conn.execute("SELECT count(*) FROM app.runs WHERE id=%s", (h.run,)).fetchone() == (
            1,
        )
    await finish(h)
    with psycopg.connect(h.database["operator"]) as conn:
        assert conn.execute(
            "SELECT title FROM app.conversations WHERE id=%s", (other,)
        ).fetchone() == ("Keep this conversation",)
        assert conn.execute(
            "SELECT count(*) FROM app.deletion_tombstones WHERE workspace_id=%s AND entity_type='workflow_thread' AND purged_at IS NOT NULL",
            (h.wid,),
        ).fetchone() == (28,)
        assert conn.execute("SELECT attempts FROM app.jobs WHERE id=%s", (h.job,)).fetchone() == (
            1,
        )


async def test_dispatch_function_is_private_and_final_receipt_is_idempotent(purge_data):
    h = purge_data
    with psycopg.connect(h.database["ed_app"], autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM app.claim_conversation_purge('forged')")
    delete(h)
    await finish(h)
    with psycopg.connect(h.database["operator"]) as conn:
        conn.execute("UPDATE app.jobs SET status='queued' WHERE id=%s", (h.job,))
    await step(h)
    with psycopg.connect(h.database["operator"]) as conn:
        assert conn.execute("SELECT status FROM app.jobs WHERE id=%s", (h.job,)).fetchone() == (
            "succeeded",
        )


async def test_provider_response_is_scrubbed_even_while_checkpoint_gate_is_busy(purge_data):
    h = purge_data
    delete(h)
    await step(h)
    async with thread_gate(h.database["ed_checkpoint"], h.threads[0]):
        await step(h)
        with psycopg.connect(h.database["operator"]) as conn:
            assert conn.execute(
                "SELECT response,cost_micro_usd,run_id FROM app.model_usage WHERE id=%s", (h.usage,)
            ).fetchone() == (None, 7, h.run)
            assert conn.execute(
                "SELECT purged_at FROM app.deletion_tombstones WHERE workspace_id=%s AND entity_type='conversation'",
                (h.wid,),
            ).fetchone() == (None,)
    await finish(h)

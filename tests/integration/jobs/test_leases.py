import asyncio
from uuid import uuid4

import psycopg
import pytest
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext, engine, tenant_transaction
from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.workflows.thread_gate import thread_gate

pytestmark = pytest.mark.integration


@pytest.fixture
def queued_job(database):
    workspace, actor, job = uuid4(), uuid4(), uuid4()
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.users(id,issuer,subject,email,display_name) VALUES(%s,%s,%s,%s,%s)",
            (actor, "test", str(actor), f"{actor}@example.test", "Worker"),
        )
        conn.execute("INSERT INTO app.workspaces(id,name) VALUES(%s,%s)", (workspace, "Leases"))
        conn.execute(
            "INSERT INTO app.memberships(workspace_id,user_id,role) VALUES(%s,%s,'owner')",
            (workspace, actor),
        )
        conn.execute(
            "INSERT INTO app.jobs(workspace_id,id,kind,status,requested_by,dedupe_key) VALUES(%s,%s,'maintenance','queued',%s,%s)",
            (workspace, job, actor, str(job)),
        )
    yield workspace, actor, job
    with psycopg.connect(database["operator"]) as conn:
        conn.execute("DELETE FROM app.workspaces WHERE id=%s", (workspace,))


async def test_claim_race_reclaim_and_stale_fence(database, queued_job):
    dispatch, app = engine(database["ed_dispatch"]), engine(database["ed_app"])
    repository = JobRepository(dispatch)
    workspace, actor, job = queued_job
    try:
        results = await asyncio.gather(
            repository.claim("maintenance", "one"), repository.claim("maintenance", "two")
        )
        leases = [lease for lease in results if lease is not None]
        assert len(leases) == 1
        first = leases[0]
        assert first.job_id == job and first.lease_token == 1
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.jobs SET lease_until=now()-interval '1 second' WHERE workspace_id=%s AND id=%s",
                (workspace, job),
            )
        with pytest.raises(LeaseLost):
            await repository.renew(first)
        assert await repository.sweep() == 1
        second = await repository.claim("maintenance", "replacement")
        assert second and second.lease_token == 2
        ctx = AuthContext(actor, workspace, "owner", 1, uuid4())
        with pytest.raises(LeaseLost):
            async with tenant_transaction(app, ctx) as conn:
                await repository.checkpoint(conn, first, {"stage": "stale"}, 50)
        async with tenant_transaction(app, ctx) as conn:
            await repository.checkpoint(conn, second, {"stage": "verified"}, 75)
            await repository.finish(conn, second)
        with pytest.raises(LeaseLost):
            await repository.renew(second)
    finally:
        await dispatch.dispose()
        await app.dispose()


async def test_thread_gate_survives_business_lease_takeover(database):
    thread = uuid4()
    async with thread_gate(database["ed_checkpoint"], thread):
        with pytest.raises(Problem, match="workflow_busy"):
            async with thread_gate(database["ed_checkpoint"], thread):
                raise AssertionError("Concurrent invocation entered")
    async with thread_gate(database["ed_checkpoint"], thread):
        pass


async def test_heartbeat_loss_cancels_stage(database, queued_job):
    from evidencedesk.jobs.lease import heartbeat

    dispatch = engine(database["ed_dispatch"])
    repository = JobRepository(dispatch)
    try:
        lease = await repository.claim("maintenance", "heartbeat")
        assert lease
        with pytest.raises(LeaseLost):
            async with heartbeat(repository, lease, interval=0.01):
                with psycopg.connect(database["operator"]) as conn:
                    conn.execute(
                        "UPDATE app.jobs SET lease_until=now()-interval '1 second' WHERE workspace_id=%s AND id=%s",
                        (lease.workspace_id, lease.job_id),
                    )
                await asyncio.sleep(1)
    finally:
        await repository.sweep()
        await dispatch.dispose()


async def test_killed_claiming_process_is_reclaimed(database, queued_job):
    import json
    import os
    import subprocess
    from pathlib import Path

    code = """import asyncio, json, os
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import engine
async def main():
 db=engine(os.environ['TEST_DISPATCH_DSN'])
 lease=await JobRepository(db).claim('maintenance','killed-process')
 print(json.dumps({'job':str(lease.job_id),'token':lease.lease_token}),flush=True)
 await asyncio.Event().wait()
asyncio.run(main())
"""
    process = subprocess.Popen(
        [str(Path.cwd() / ".venv/bin/python"), "-c", code],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "TEST_DISPATCH_DSN": database["ed_dispatch"]},
    )
    dispatch = engine(database["ed_dispatch"])
    try:
        claimed = json.loads(await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 5))
        process.kill()
        process.wait(timeout=5)
        workspace, _, job = queued_job
        assert claimed["job"] == str(job)
        # Advance only the persisted lease deadline; do not wait 30 wall-clock seconds.
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.jobs SET lease_until=now()-interval '1 second' WHERE workspace_id=%s AND id=%s",
                (workspace, job),
            )
        repository = JobRepository(dispatch)
        await repository.sweep()
        recovered = await repository.claim("maintenance", "replacement-process")
        assert (
            recovered and recovered.job_id == job and recovered.lease_token == claimed["token"] + 1
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        await dispatch.dispose()


async def test_index_outbox_claim_and_takeover(database, queued_job):
    workspace, actor, _ = queued_job
    kb, doc, revision, outbox = uuid4(), uuid4(), uuid4(), uuid4()
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.knowledge_bases(workspace_id,id,name,created_by) VALUES(%s,%s,%s,%s)",
            (workspace, kb, "Index", actor),
        )
        conn.execute(
            "INSERT INTO app.documents(workspace_id,id,kb_id,title,created_by) VALUES(%s,%s,%s,%s,%s)",
            (workspace, doc, kb, "Document", actor),
        )
        conn.execute(
            "INSERT INTO app.document_revisions(workspace_id,id,document_id,kb_id,revision_no,status,object_key,mime_type,byte_size,content_sha256,parser_version,chunk_profile_id,profile_hash,embedding_profile_id) VALUES(%s,%s,%s,%s,1,'embedding','fixture','text/plain',1,%s,'v1','v1','v1','bge_small_en_v15')",
            (workspace, revision, doc, kb, "a" * 64),
        )
        conn.execute(
            "INSERT INTO app.index_manifests(workspace_id,revision_id,profile_id,collection_name,state,expected_points,manifest_sha256,artifact_key) VALUES(%s,%s,'bge_small_en_v15','fixture','staged',1,%s,'fixture')",
            (workspace, revision, "b" * 64),
        )
        conn.execute(
            "INSERT INTO app.index_outbox(workspace_id,id,revision_id,profile_id,operation,operation_key) VALUES(%s,%s,%s,'bge_small_en_v15','upsert',%s)",
            (workspace, outbox, revision, str(outbox)),
        )
    with psycopg.connect(database["ed_dispatch"]) as conn:
        first = conn.execute("SELECT * FROM app.claim_index_outbox()").fetchone()
        assert first[1] == outbox and first[-1] == 1
        assert conn.execute("SELECT * FROM app.claim_index_outbox()").fetchone() is None
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.index_outbox SET lease_until=now()-interval '1 second' WHERE workspace_id=%s AND id=%s",
            (workspace, outbox),
        )
    with psycopg.connect(database["ed_dispatch"]) as conn:
        recovered = conn.execute("SELECT * FROM app.claim_index_outbox()").fetchone()
        assert recovered[1] == outbox and recovered[-1] == 2

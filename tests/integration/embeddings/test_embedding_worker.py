import asyncio
import threading

import psycopg
import pytest
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.model_slots import local_generation_slot
from evidencedesk.db.session import engine
from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.ingestion.sandbox import ParserSandbox
from evidencedesk.workers.embedding import EmbeddingWorker, cpu_call
from evidencedesk.workers.ingest import ExtractionWorker

from tests.security.uploads.test_uploads import setup, upload

pytestmark = pytest.mark.integration


async def test_staged_vectors_private_and_replayed_without_embedding(
    auth_client, object_store, database, parser_image, encoder, monkeypatch
):
    client, _, _, h, base, url = setup(auth_client)
    accepted = upload(client, url, h, ("Evidence source passage. " * 900).encode()).json()
    db, dispatch = engine(database["ed_app"]), engine(database["ed_dispatch"])
    jobs = JobRepository(dispatch)
    try:
        lease = await jobs.claim("ingest", "vector-fixture")
        parsed = await ExtractionWorker(db, jobs, object_store, ParserSandbox(parser_image)).run(
            lease
        )
        worker = EmbeddingWorker(db, jobs, object_store, encoder)
        artifact = await worker.run(lease, parsed)
        assert len(artifact.points) > 1
        assert all("text" not in p.model_dump() for p in artifact.points)
        with psycopg.connect(database["operator"]) as conn:
            row = conn.execute(
                "SELECT chunk_count,status FROM app.document_revisions WHERE id=%s",
                (lease.revision_id,),
            ).fetchone()
            assert row == (len(artifact.points), "verifying")
            assert conn.execute(
                "SELECT count(*) FROM app.chunks WHERE revision_id=%s", (lease.revision_id,)
            ).fetchone()[0] == len(artifact.points)
            assert (
                conn.execute(
                    "SELECT active_revision_id FROM app.documents WHERE id=%s",
                    (accepted["document"]["id"],),
                ).fetchone()[0]
                is None
            )
            conn.execute(
                "UPDATE app.jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                (lease.job_id,),
            )
        await jobs.sweep()
        renewed = await jobs.claim("ingest", "vector-resume")

        def no_embedding(*args):
            raise AssertionError("A verified vector artifact must be reused")

        monkeypatch.setattr(encoder, "embed", no_embedding)
        restored = await worker.run(renewed, parsed)
        assert artifact == restored
        assert len(object_store.list_page("workspaces/")[0]) == 3
        assert client.get(base + "/jobs/" + str(lease.job_id)).json()["stage"] == "verifying"
    finally:
        await db.dispose()
        await dispatch.dispose()


async def test_chunk_tampering_fails_closed(
    auth_client, object_store, database, parser_image, encoder
):
    client, _, _, h, _, url = setup(auth_client)
    upload(client, url, h)
    db, dispatch = engine(database["ed_app"]), engine(database["ed_dispatch"])
    jobs = JobRepository(dispatch)
    try:
        lease = await jobs.claim("ingest", "tamper-fixture")
        parsed = await ExtractionWorker(db, jobs, object_store, ParserSandbox(parser_image)).run(
            lease
        )
        worker = EmbeddingWorker(db, jobs, object_store, encoder)
        await worker.run(lease, parsed)
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.chunks SET embedding_text='unexpected source' WHERE revision_id=%s",
                (lease.revision_id,),
            )
        with pytest.raises(Problem) as exc:
            await worker.run(lease, parsed)
        assert exc.value.code == "chunk_integrity_failure"
        with psycopg.connect(database["operator"]) as conn:
            assert (
                conn.execute("SELECT status FROM app.jobs WHERE id=%s", (lease.job_id,)).fetchone()[
                    0
                ]
                == "dead"
            )
    finally:
        await db.dispose()
        await dispatch.dispose()


async def test_embedding_slot_busy_defers_without_consuming_attempt(
    auth_client, object_store, database, parser_image, encoder
):
    client, _, _, h, _, url = setup(auth_client)
    upload(client, url, h)
    db, dispatch = engine(database["ed_app"]), engine(database["ed_dispatch"])
    jobs = JobRepository(dispatch)
    try:
        lease = await jobs.claim("ingest", "busy-fixture")
        parsed = await ExtractionWorker(db, jobs, object_store, ParserSandbox(parser_image)).run(
            lease
        )
        async with local_generation_slot(db, slot_id=2):
            with pytest.raises(Problem) as exc:
                await EmbeddingWorker(db, jobs, object_store, encoder).run(lease, parsed)
        assert exc.value.code == "local_model_busy"
        with psycopg.connect(database["operator"]) as conn:
            assert conn.execute(
                "SELECT status,attempts FROM app.jobs WHERE id=%s", (lease.job_id,)
            ).fetchone() == ("retry_wait", 0)
    finally:
        await db.dispose()
        await dispatch.dispose()


async def test_delete_during_embedding_prevents_vector_checkpoint(
    auth_client, object_store, database, parser_image, encoder, monkeypatch
):
    client, _, _, h, base, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    db, dispatch = engine(database["ed_app"]), engine(database["ed_dispatch"])
    jobs = JobRepository(dispatch)
    original = encoder.embed

    def deleting(chunks):
        points = original(chunks)
        assert (
            client.delete(
                base + "/knowledge-bases/" + accepted["document"]["kb_id"], headers=h
            ).status_code
            == 202
        )
        return points

    try:
        lease = await jobs.claim("ingest", "delete-fixture")
        parsed = await ExtractionWorker(db, jobs, object_store, ParserSandbox(parser_image)).run(
            lease
        )
        monkeypatch.setattr(encoder, "embed", deleting)
        with pytest.raises((LeaseLost, Problem)):
            await EmbeddingWorker(db, jobs, object_store, encoder).run(lease, parsed)
        assert not any("/manifest/" in o.key for o in object_store.list_page("workspaces/")[0])
        with psycopg.connect(database["operator"]) as conn:
            assert (
                conn.execute("SELECT status FROM app.jobs WHERE id=%s", (lease.job_id,)).fetchone()[
                    0
                ]
                == "cancelled"
            )
    finally:
        await db.dispose()
        await dispatch.dispose()


async def test_cancellation_waits_for_cpu_thread_even_when_cancelled_twice():
    started, finish = threading.Event(), threading.Event()

    def operation():
        started.set()
        finish.wait(5)
        return 1

    task = asyncio.create_task(cpu_call(operation))
    try:
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task

import hashlib
from uuid import uuid4

import psycopg
import pytest
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import engine
from evidencedesk.errors import Problem
from evidencedesk.ingestion.sandbox import ParserSandbox
from evidencedesk.workers.ingest import ExtractionWorker

from tests.security.uploads.test_uploads import setup, upload

pytestmark = pytest.mark.integration


async def test_ten_golden_fixtures_in_real_container(parser_image, golden_sources, tmp_path):
    sandbox = ParserSandbox(parser_image)
    for index, (source, expected) in enumerate(golden_sources):
        source.chmod(0o444)
        from evidencedesk.ingestion.validation import MIMES

        result = await sandbox.parse(
            source,
            source.name,
            MIMES[source.suffix],
            hashlib.sha256(source.read_bytes()).hexdigest(),
            tmp_path / f"blocks-{index}.json",
        )
        assert [b.text for b in result.blocks] == expected, source.name


async def test_durable_extraction_resumes_without_reparse_and_progress_is_scoped(
    auth_client, object_store, database, parser_image, monkeypatch
):
    client, state, _, h, base, url = setup(auth_client)
    accepted = upload(
        client,
        url,
        h,
        b"# Evidence\n\nAlways cite original sources.",
        name="policy.md",
        mime="text/markdown",
    ).json()
    db, dispatch = engine(database["ed_app"]), engine(database["ed_dispatch"])
    jobs = JobRepository(dispatch)
    try:
        lease = await jobs.claim("ingest", "extract-fixture")
        assert lease
        parser = ParserSandbox(parser_image)
        worker = ExtractionWorker(db, jobs, object_store, parser)
        artifact = await worker.run(lease)
        assert artifact.manifest.blocks[-1].text == "Always cite original sources."
        progress = client.get(base + "/jobs/" + str(lease.job_id)).json()
        assert progress["stage"] == "chunking" and progress["progress"] == 30
        assert "checkpoint" not in progress and "blocks_key" not in progress
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                (lease.job_id,),
            )
        assert await jobs.sweep() == 1
        resumed = await jobs.claim("ingest", "resumed-fixture")
        assert resumed and resumed.lease_token > lease.lease_token

        async def forbidden_reparse(*args, **kwargs):
            raise AssertionError("Verified extraction must be reused")

        monkeypatch.setattr(parser, "parse", forbidden_reparse)
        restored = await worker.run(resumed)
        assert restored == artifact
        keys = [o.key for o in object_store.list_page("workspaces/")[0]]
        assert sum("/blocks/" in key for key in keys) == 1
        document = client.get(base + "/documents/" + accepted["document"]["id"]).json()
        assert document["active_revision_id"] is None and document["status"] == "chunking"
        state["subject"] = str(uuid4())
        from tests.support import sign_in

        sign_in(client, state)
        assert client.get(base + "/jobs/" + str(lease.job_id)).status_code == 404
    finally:
        await db.dispose()
        await dispatch.dispose()


async def test_artifact_before_checkpoint_crash_replays_idempotently(
    auth_client, object_store, database, parser_image, monkeypatch
):
    from evidencedesk.db import ingestion_repository

    client, _, _, h, _, url = setup(auth_client)
    upload(client, url, h)
    db, dispatch = engine(database["ed_app"]), engine(database["ed_dispatch"])
    jobs = JobRepository(dispatch)
    original_stage = ingestion_repository.stage

    async def crash(conn, ctx, lease, name, checkpoint, progress):
        if name == "chunking":
            raise RuntimeError("Crash after object PUT before checkpoint COMMIT")
        await original_stage(conn, ctx, lease, name, checkpoint, progress)

    try:
        lease = await jobs.claim("ingest", "crash-fixture")
        worker = ExtractionWorker(db, jobs, object_store, ParserSandbox(parser_image))
        monkeypatch.setattr(ingestion_repository, "stage", crash)
        with pytest.raises(RuntimeError):
            await worker.run(lease)
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                (lease.job_id,),
            )
        await jobs.sweep()
        monkeypatch.setattr(ingestion_repository, "stage", original_stage)
        resumed = await jobs.claim("ingest", "recovered-fixture")
        result = await worker.run(resumed)
        assert result.manifest.blocks[0].text == "A safe document."
        assert len(object_store.list_page("workspaces/")[0]) == 2
    finally:
        await db.dispose()
        await dispatch.dispose()


async def test_parse_failure_safe_progress_explicit_retry_and_tombstone(
    auth_client, object_store, database, parser_image
):
    client, _, _, h, base, url = setup(auth_client)
    accepted = upload(
        client, url, h, b"%PDF-1.7 malformed fixture", "bad.pdf", "application/pdf"
    ).json()
    db, dispatch = engine(database["ed_app"]), engine(database["ed_dispatch"])
    jobs = JobRepository(dispatch)
    try:
        lease = await jobs.claim("ingest", "invalid-fixture")
        with pytest.raises(Problem) as exc:
            await ExtractionWorker(db, jobs, object_store, ParserSandbox(parser_image)).run(lease)
        assert exc.value.code == "parse_failed"
        route = base + "/jobs/" + str(lease.job_id)
        progress = client.get(route).json()
        assert progress["status"] == "dead" and progress["error_code"] == "parse_failed"
        assert (
            client.get(base + "/documents/" + accepted["document"]["id"]).json()["status"]
            == "failed"
        )
        retried = client.post(route + "/retry", headers=h)
        assert retried.status_code == 202, retried.text
        assert client.post(route + "/retry", headers=h).json() == retried.json()
        with psycopg.connect(database["operator"]) as conn:
            row = conn.execute(
                "SELECT attempts,generation,lease_token FROM app.jobs WHERE id=%s", (lease.job_id,)
            ).fetchone()
            assert row[0] == 0 and row[1] == 2 and row[2] > lease.lease_token
        assert (
            client.post(route + "/retry", headers={**h, "Idempotency-Key": uuid4().hex}).status_code
            == 409
        )
        kb = accepted["document"]["kb_id"]
        assert client.delete(base + "/knowledge-bases/" + kb, headers=h).status_code == 202
        assert client.get(route).status_code == 404
        assert client.post(route + "/retry", headers=h).status_code == 404
    finally:
        await db.dispose()
        await dispatch.dispose()


async def test_exhausted_ingestion_lease_can_be_explicitly_retried(
    auth_client, object_store, database
):
    client, _, _, h, base, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    dispatch = engine(database["ed_dispatch"])
    jobs = JobRepository(dispatch)
    try:
        lease = await jobs.claim("ingest", "exhausted-fixture")
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.jobs SET attempts=3,lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                (lease.job_id,),
            )
            conn.execute(
                "UPDATE app.document_revisions SET status='parsing' WHERE id=%s",
                (accepted["revision_id"],),
            )
        assert await jobs.sweep() == 1
        document = client.get(base + "/documents/" + accepted["document"]["id"]).json()
        assert document["status"] == "failed" and document["active_revision_id"] is None
        assert (
            client.post(base + "/jobs/" + str(lease.job_id) + "/retry", headers=h).status_code
            == 202
        )
    finally:
        await dispatch.dispose()

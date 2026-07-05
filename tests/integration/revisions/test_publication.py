import hashlib
from uuid import UUID, uuid4

import psycopg
import pytest
from evidencedesk.db.index_repository import OutboxRepository
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext, engine
from evidencedesk.embeddings.models import SparseVector, VectorArtifact, VectorPoint
from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.ingestion.profile import IngestionProfile
from evidencedesk.ingestion.publish import Publisher
from evidencedesk.ingestion.sandbox import ParserSandbox
from evidencedesk.workers.embedding import EmbeddingWorker
from evidencedesk.workers.index import IndexWorker
from evidencedesk.workers.ingest import ExtractionWorker
from psycopg.types.json import Jsonb
from qdrant_client import models

from tests.security.uploads.test_uploads import setup, upload

pytestmark = pytest.mark.integration


@pytest.fixture
async def services(database, object_store, writer):
    db, dispatch = engine(database["ed_app"]), engine(database["ed_dispatch"])
    jobs, outbox = JobRepository(dispatch), OutboxRepository(dispatch)
    try:
        yield (
            jobs,
            outbox,
            Publisher(db, jobs, object_store, writer.config),
            IndexWorker(db, outbox, object_store, writer),
        )
    finally:
        await db.dispose()
        await dispatch.dispose()


def scope(lease):
    return AuthContext(lease.actor_id, lease.workspace_id, "editor", 0, uuid4())


def fixture_vectors(database, store, lease, accepted, tmp_path, n=3, *, texts=None):
    """Synthetic numerical fixture at the already-tested extraction/embedding stage boundary."""
    profile = IngestionProfile()
    texts = texts if texts is not None else [f"Fixture evidence passage {i}." for i in range(n)]
    n = len(texts)
    points = [
        VectorPoint(
            id=uuid4(),
            ordinal=i,
            text_sha256=hashlib.sha256(t.encode()).hexdigest(),
            dense=[1.0 if j == i % 384 else 0.0 for j in range(384)],
            sparse=SparseVector(indices=[i + 1], values=[1.0]),
        )
        for i, t in enumerate(texts)
    ]
    artifact = VectorArtifact(
        workspace_id=lease.workspace_id,
        kb_id=UUID(accepted["document"]["kb_id"]),
        document_id=UUID(accepted["document"]["id"]),
        revision_id=lease.revision_id,
        profile_id=profile.embedding_profile_id,
        profile_hash=profile.fingerprint,
        blocks_sha256="a" * 64,
        points=points,
    )
    key = f"workspaces/{lease.workspace_id}/documents/{artifact.document_id}/revisions/{lease.revision_id}/manifest/{artifact.sha256}"
    path = tmp_path / "vectors.json"
    path.write_bytes(artifact.canonical_bytes())
    store.put(key, path, "application/json", artifact.sha256)
    with psycopg.connect(database["operator"]) as conn:
        for p, t in zip(points, texts, strict=True):
            conn.execute(
                """INSERT INTO app.chunks(workspace_id,id,document_id,revision_id,kb_id,ordinal,text,
                embedding_text,text_sha256,token_count,char_start,char_end,source_locator)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,8,%s,%s,'{}')""",
                (
                    lease.workspace_id,
                    p.id,
                    artifact.document_id,
                    lease.revision_id,
                    artifact.kb_id,
                    p.ordinal,
                    t,
                    t,
                    p.text_sha256,
                    p.ordinal * 30,
                    p.ordinal * 30 + len(t),
                ),
            )
        conn.execute(
            "UPDATE app.document_revisions SET status='verifying',chunk_count=%s WHERE id=%s",
            (n, lease.revision_id),
        )
        conn.execute(
            "UPDATE app.jobs SET checkpoint=%s WHERE id=%s",
            (
                Jsonb(
                    {
                        "vectors_key": key,
                        "vectors_sha256": artifact.sha256,
                        "blocks_sha256": artifact.blocks_sha256,
                        "chunk_count": n,
                        "stage": "verifying",
                    }
                ),
                lease.job_id,
            ),
        )
    return artifact


def active(database, doc):
    with psycopg.connect(database["operator"]) as conn:
        return conn.execute(
            "SELECT active_revision_id FROM app.documents WHERE id=%s", (doc,)
        ).fetchone()[0]


def due(database):
    with psycopg.connect(database["operator"]) as conn:
        conn.execute("UPDATE app.index_outbox SET next_attempt_at=now() WHERE state='pending'")
        conn.execute(
            "UPDATE app.jobs SET available_at=now() WHERE kind='ingest' AND status='retry_wait'"
        )


async def noop():
    pass


async def test_complete_real_extraction_embedding_index_publish(
    auth_client, object_store, database, services, parser_image, encoder, writer
):
    client, _, _, h, base, url = setup(auth_client)
    accepted = upload(
        client, url, h, b"This is actual extracted and embedded evidence for verified publication."
    ).json()
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "end-to-end")
    parsed = await ExtractionWorker(
        publisher.db, jobs, object_store, ParserSandbox(parser_image)
    ).run(lease)
    artifact = await EmbeddingWorker(publisher.db, jobs, object_store, encoder).run(lease, parsed)
    assert await publisher.run(lease) is False
    assert active(database, accepted["document"]["id"]) is None
    claim = await outbox.claim()
    assert claim
    await delivery.run(claim)
    assert active(database, accepted["document"]["id"]) is None  # verified != published
    renewed = await jobs.claim("ingest", "finalizer")
    assert renewed
    assert await publisher.run(renewed) is True
    assert active(database, accepted["document"]["id"]) == lease.revision_id
    await writer.verify(artifact, artifact.sha256, noop)
    assert client.get(base + "/jobs/" + str(lease.job_id)).json()["status"] == "succeeded"
    with pytest.raises(LeaseLost):
        await publisher.finalize(scope(lease), lease)


async def test_lost_ack_and_expired_outbox_replays_without_duplicates(
    auth_client, object_store, database, services, writer, tmp_path, monkeypatch
):
    client, _, _, h, _, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "fixture")
    artifact = fixture_vectors(database, object_store, lease, accepted, tmp_path, 103)
    await publisher.stage(scope(lease), lease)
    first = await outbox.claim()
    assert first
    original = writer.upsert

    async def lost(*args):
        await original(*args)
        raise TimeoutError("acknowledgement lost after server applied points")

    monkeypatch.setattr(writer, "upsert", lost)
    with pytest.raises(Problem, match="index_unavailable"):
        await delivery.run(first)
    assert active(database, artifact.document_id) is None
    monkeypatch.setattr(writer, "upsert", original)
    due(database)
    second = await outbox.claim()
    assert second.lease_token > first.lease_token
    await outbox.renew(second)
    with pytest.raises(LeaseLost):
        await outbox.renew(first)
    await delivery.run(second)
    await writer.verify(artifact, artifact.sha256, noop)
    assert await publisher.finalize(scope(lease), lease)
    assert (await writer.client.count(writer.config.collection, exact=True)).count == 103


@pytest.mark.parametrize("fault", ["missing", "extra", "hash", "vector", "tenant"])
async def test_readback_corruption_blocks_publication(
    auth_client, object_store, database, services, writer, tmp_path, monkeypatch, fault
):
    client, _, _, h, _, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "corruption")
    artifact = fixture_vectors(database, object_store, lease, accepted, tmp_path)
    await publisher.stage(scope(lease), lease)
    original = writer.upsert

    async def corrupt(*args):
        await original(*args)
        p = artifact.points[0]
        name = writer.config.collection
        if fault == "missing":
            await writer.client.delete(name, [str(p.id)], wait=True)
        elif fault == "extra":
            await writer.client.upsert(
                name,
                [
                    models.PointStruct(
                        id=str(uuid4()),
                        vector={
                            "dense": p.dense,
                            "bm25": models.SparseVector(
                                indices=p.sparse.indices, values=p.sparse.values
                            ),
                        },
                        payload=writer.payload(artifact, p, artifact.sha256),
                    )
                ],
                wait=True,
            )
        elif fault in ("hash", "tenant"):
            await writer.client.set_payload(
                name,
                {"text_sha256": "0" * 64} if fault == "hash" else {"workspace_id": str(uuid4())},
                [str(p.id)],
                wait=True,
            )
        else:
            await writer.client.update_vectors(
                name,
                [models.PointVectors(id=str(p.id), vector={"dense": artifact.points[1].dense})],
                wait=True,
            )

    monkeypatch.setattr(writer, "upsert", corrupt)
    with pytest.raises(Problem, match="index_"):
        await delivery.run(await outbox.claim())
    assert active(database, artifact.document_id) is None
    with pytest.raises(Problem, match="index_delivery_failed"):
        await publisher.finalize(scope(lease), lease)


@pytest.mark.parametrize("parent", ["knowledge_base", "workspace"])
async def test_tombstone_fences_delayed_upsert_and_purges_exact_scope(
    auth_client, object_store, database, services, writer, tmp_path, monkeypatch, parent
):
    client, _, _, h, base, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "delete")
    artifact = fixture_vectors(database, object_store, lease, accepted, tmp_path)
    await publisher.stage(scope(lease), lease)
    claim = await outbox.claim()
    original = writer.upsert

    async def deleted(*args):
        endpoint = (
            base if parent == "workspace" else base + "/knowledge-bases/" + str(artifact.kb_id)
        )
        assert client.delete(endpoint, headers=h).status_code == 202
        # Simulates a request that had already passed its DB guard before deletion committed.
        await original(artifact, artifact.sha256, noop)

    monkeypatch.setattr(writer, "upsert", deleted)
    with pytest.raises(LeaseLost):
        await delivery.run(claim)
    with pytest.raises(Problem):
        await publisher.finalize(scope(lease), lease)
    assert active(database, artifact.document_id) is None

    purge = await outbox.claim()
    assert purge.operation == "purge"
    await delivery.run(purge)
    assert (await writer.client.count(writer.config.collection, exact=True)).count == 0


async def test_publication_and_delivery_commit_failures_are_recoverable(
    auth_client, object_store, database, services, writer, tmp_path, monkeypatch
):
    from evidencedesk.db import index_repository

    client, _, _, h, _, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "commit-failure")
    artifact = fixture_vectors(database, object_store, lease, accepted, tmp_path)
    await publisher.stage(scope(lease), lease)
    claim = await outbox.claim()
    complete = index_repository.complete

    async def fail_complete(*args):
        await complete(*args)
        raise RuntimeError("crash before index verification transaction commits")

    monkeypatch.setattr(index_repository, "complete", fail_complete)
    with pytest.raises(RuntimeError):
        await delivery.run(claim)
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT state FROM app.index_manifests WHERE revision_id=%s", (lease.revision_id,)
            ).fetchone()[0]
            == "indexing"
        )
    assert active(database, artifact.document_id) is None
    monkeypatch.setattr(index_repository, "complete", complete)
    await delivery.run(claim)
    finish = JobRepository.finish

    async def fail_finish(*args):
        await finish(*args)
        raise RuntimeError("crash before publication transaction commits")

    monkeypatch.setattr(JobRepository, "finish", fail_finish)
    with pytest.raises(RuntimeError):
        await publisher.finalize(scope(lease), lease)
    assert active(database, artifact.document_id) is None
    monkeypatch.setattr(JobRepository, "finish", finish)
    assert await publisher.finalize(scope(lease), lease)
    assert active(database, artifact.document_id) == lease.revision_id


async def test_failed_replacement_preserves_old_active_and_cannot_beat_newer_revision(
    auth_client, object_store, database, services, writer, tmp_path, monkeypatch
):
    client, _, _, h, base, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    jobs, outbox, publisher, delivery = services
    first = await jobs.claim("ingest", "first")
    artifact = fixture_vectors(database, object_store, first, accepted, tmp_path)
    await publisher.stage(scope(first), first)
    await delivery.run(await outbox.claim())
    assert await publisher.finalize(scope(first), first)
    revisions_url = base + "/documents/" + str(artifact.document_id) + "/revisions"
    second_response = upload(
        client, revisions_url, {**h, "Idempotency-Key": str(uuid4())}, b"New edition."
    ).json()
    second = await jobs.claim("ingest", "second")
    fixture_vectors(database, object_store, second, second_response, tmp_path)
    await publisher.stage(scope(second), second)
    assert active(database, artifact.document_id) == first.revision_id
    original = writer.verify

    async def invalid(*args):
        raise Problem(422, "index_vector_mismatch", "Scripted incomplete replacement.")

    monkeypatch.setattr(writer, "verify", invalid)
    with pytest.raises(Problem):
        await delivery.run(await outbox.claim())
    with pytest.raises(Problem, match="index_delivery_failed"):
        await publisher.run(second)
    assert active(database, artifact.document_id) == first.revision_id
    third_response = upload(
        client, revisions_url, {**h, "Idempotency-Key": str(uuid4())}, b"Newest edition."
    ).json()
    third = await jobs.claim("ingest", "third")
    fixture_vectors(database, object_store, third, third_response, tmp_path)
    await publisher.stage(scope(third), third)
    with pytest.raises(LeaseLost):
        await publisher.finalize(scope(second), second)
    monkeypatch.setattr(writer, "verify", original)
    await delivery.run(await outbox.claim())
    assert await publisher.finalize(scope(third), third)
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT status FROM app.document_revisions WHERE id=%s", (first.revision_id,)
            ).fetchone()[0]
            == "superseded"
        )
        assert (
            conn.execute(
                "SELECT corpus_generation FROM app.workspaces WHERE id=%s", (first.workspace_id,)
            ).fetchone()[0]
            == 3
        )


@pytest.mark.parametrize("fault", ["stored_artifact", "chunk_after_verify"])
async def test_immutable_content_tampering_never_publishes(
    auth_client, object_store, database, services, writer, tmp_path, fault
):
    client, _, _, h, _, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "content-integrity")
    artifact = fixture_vectors(database, object_store, lease, accepted, tmp_path)
    if fault == "stored_artifact":
        key = f"workspaces/{lease.workspace_id}/documents/{artifact.document_id}/revisions/{lease.revision_id}/manifest/{artifact.sha256}"
        object_store.client.put_object(Bucket=object_store.bucket, Key=key, Body=b"corrupt")
        with pytest.raises(Problem, match="object_checksum_mismatch"):
            await publisher.run(lease)
    else:
        await publisher.stage(scope(lease), lease)
        await delivery.run(await outbox.claim())
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.chunks SET text='changed after vector verification' WHERE id=%s",
                (artifact.points[0].id,),
            )
        with pytest.raises(Problem, match="chunk_integrity_failure"):
            await publisher.finalize(scope(lease), lease)
    assert active(database, artifact.document_id) is None


async def test_version_change_and_revoked_uploader_cannot_publish(
    auth_client, object_store, database, services, writer, tmp_path
):
    client, _, identity, h, _, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "version")
    artifact = fixture_vectors(database, object_store, lease, accepted, tmp_path)
    await publisher.stage(scope(lease), lease)
    await delivery.run(await outbox.claim())
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.documents SET version=version+1 WHERE id=%s", (artifact.document_id,)
        )
    with pytest.raises(Problem, match="publication_changed"):
        await publisher.finalize(scope(lease), lease)
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.memberships SET role='viewer' WHERE workspace_id=%s AND user_id=%s",
            (lease.workspace_id, lease.actor_id),
        )
    with pytest.raises(Problem, match="forbidden"):
        await publisher.finalize(scope(lease), lease)
    assert active(database, artifact.document_id) is None

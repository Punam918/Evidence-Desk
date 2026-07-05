from uuid import uuid4

import psycopg
import pytest
from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.index.qdrant import client as index_client
from evidencedesk.jobs.lease import heartbeat
from evidencedesk.lifecycle.reconcile import Reconciler
from qdrant_client import models
from qdrant_client.http.exceptions import UnexpectedResponse

from tests.integration.revisions.test_publication import (
    active,
    due,
    fixture_vectors,
    noop,
    scope,
)
from tests.integration.revisions.test_publication import (
    services as services,
)
from tests.security.uploads.test_uploads import setup, upload

pytestmark = pytest.mark.integration


async def test_collection_loss_rebuilds_frozen_generation_without_model_calls(
    auth_client, object_store, database, services, writer, tmp_path
):
    client, _, _, h, _, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "rebuild")
    artifact = fixture_vectors(database, object_store, lease, accepted, tmp_path)
    await publisher.stage(scope(lease), lease)
    await delivery.run(await outbox.claim())
    assert await publisher.finalize(scope(lease), lease)
    await writer.client.delete_collection(writer.config.collection)
    reconciler = Reconciler(publisher.db, outbox, object_store, writer)
    audit = await reconciler.audit_workspace(lease.workspace_id)
    assert (audit.checked, audit.repairs, audit.failures) == (1, 1, 1)
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT state FROM app.index_manifests WHERE revision_id=%s", (lease.revision_id,)
            ).fetchone()[0]
            == "unavailable"
        )
        # Rebuilding a published index must survive the original uploader leaving the workspace.
        conn.execute(
            "DELETE FROM app.memberships WHERE workspace_id=%s AND user_id=%s",
            (lease.workspace_id, lease.actor_id),
        )
    assert active(database, artifact.document_id) == lease.revision_id
    await delivery.run(await outbox.claim())
    await writer.verify(artifact, artifact.sha256, noop)
    again = await reconciler.audit_workspace(lease.workspace_id)
    assert (again.repairs, again.failures) == (0, 0)


async def test_stale_snapshot_missing_points_and_late_post_purge_write(
    auth_client, object_store, database, services, writer, tmp_path
):
    client, _, _, h, base, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "stale-restore")
    artifact = fixture_vectors(database, object_store, lease, accepted, tmp_path)
    await publisher.stage(scope(lease), lease)
    await delivery.run(await outbox.claim())
    assert await publisher.finalize(scope(lease), lease)
    await writer.client.delete(writer.config.collection, [str(artifact.points[0].id)], wait=True)
    reconciler = Reconciler(publisher.db, outbox, object_store, writer)
    assert (await reconciler.audit_workspace(lease.workspace_id)).repairs == 1
    await delivery.run(await outbox.claim())
    await writer.verify(artifact, artifact.sha256, noop)
    assert (
        client.delete(base + "/knowledge-bases/" + str(artifact.kb_id), headers=h).status_code
        == 202
    )
    await delivery.run(await outbox.claim())
    # An already-sent stale external request can arrive after the first purge acknowledgement.
    await writer.upsert(artifact, artifact.sha256, noop)
    assert (await reconciler.audit_workspace(lease.workspace_id)).repairs == 1
    await delivery.run(await outbox.claim())
    assert (await writer.client.count(writer.config.collection, exact=True)).count == 0


async def test_unknown_points_quarantine_and_known_ids_with_corrupt_scope_are_preserved(
    auth_client, object_store, database, services, writer, tmp_path
):
    client, _, _, h, base, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "orphans")
    artifact = fixture_vectors(database, object_store, lease, accepted, tmp_path)
    await publisher.stage(scope(lease), lease)
    await delivery.run(await outbox.claim())
    assert await publisher.finalize(scope(lease), lease)
    other = client.post(
        "/v1/workspaces",
        json={"name": "Other orphan tenant"},
        headers={**h, "Idempotency-Key": str(uuid4())},
    ).json()["id"]
    point = artifact.points[0]
    # The global ownership probe protects a known ID even when its payload falsely names another tenant.
    await writer.client.set_payload(
        writer.config.collection, {"workspace_id": other}, [str(point.id)], wait=True
    )
    stray = uuid4()
    await writer.client.upsert(
        writer.config.collection,
        [
            models.PointStruct(
                id=str(stray),
                vector={"dense": point.dense},
                payload={**writer.payload(artifact, point, artifact.sha256), "workspace_id": other},
            )
        ],
        wait=True,
    )
    from uuid import UUID

    reconciler = Reconciler(publisher.db, outbox, object_store, writer)
    assert (await reconciler.orphans(UUID(other)))[0] == 0
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM app.index_orphans WHERE workspace_id=%s", (other,)
            ).fetchone()[0]
            == 1
        )
        conn.execute(
            "UPDATE app.index_orphans SET first_seen=now()-interval '25 hours' WHERE workspace_id=%s",
            (other,),
        )
    assert (await reconciler.orphans(UUID(other)))[0] == 1
    assert len(await writer.client.retrieve(writer.config.collection, [str(point.id)])) == 1
    assert not await writer.client.retrieve(writer.config.collection, [str(stray)])
    with psycopg.connect(database["ed_app"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT app.known_chunk_ids(ARRAY[%s]::uuid[])", (point.id,))


async def test_exhausted_delivery_stays_dead_until_explicit_authorized_retry(
    auth_client, object_store, database, services, writer, tmp_path, monkeypatch
):
    client, _, _, h, base, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "exhaustion")
    artifact = fixture_vectors(database, object_store, lease, accepted, tmp_path)
    await publisher.stage(scope(lease), lease)
    original = writer.upsert

    async def unavailable(*args):
        raise TimeoutError("scripted service outage")

    monkeypatch.setattr(writer, "upsert", unavailable)
    for _ in range(3):
        claim = await outbox.claim()
        with pytest.raises(Problem, match="index_unavailable"):
            await delivery.run(claim)
        due(database)
    assert await outbox.claim() is None
    with pytest.raises(Problem, match="index_delivery_failed"):
        await publisher.run(lease)
    reconciler = Reconciler(publisher.db, outbox, object_store, writer)
    assert (await reconciler.audit_workspace(lease.workspace_id)).repairs == 0
    retry = client.post(
        base + "/jobs/" + str(lease.job_id) + "/retry",
        headers={**h, "Idempotency-Key": str(uuid4())},
    )
    assert retry.status_code == 202, retry.text
    monkeypatch.setattr(writer, "upsert", original)
    renewed = await jobs.claim("ingest", "explicit-retry")
    await publisher.stage(scope(renewed), renewed)
    await delivery.run(await outbox.claim())
    assert await publisher.finalize(scope(renewed), renewed)
    assert active(database, artifact.document_id) == lease.revision_id


async def test_outbox_expired_lease_and_heartbeat_cannot_commit(
    auth_client, object_store, database, services, writer, tmp_path
):
    import asyncio

    client, _, _, h, _, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "takeover")
    fixture_vectors(database, object_store, lease, accepted, tmp_path)
    await publisher.stage(scope(lease), lease)
    first = await outbox.claim()
    with pytest.raises(LeaseLost):
        async with heartbeat(outbox, first, interval=0.01):
            with psycopg.connect(database["operator"]) as conn:
                conn.execute(
                    "UPDATE app.index_outbox SET lease_until=now()-interval '1 second' WHERE id=%s",
                    (first.outbox_id,),
                )
            await asyncio.sleep(0.2)
    second = await outbox.claim()
    assert second.lease_token == first.lease_token + 1
    with pytest.raises(LeaseLost):
        await delivery.run(first)
    await delivery.run(second)
    assert await publisher.finalize(scope(lease), lease)


async def test_read_only_key_and_unindexed_filter_are_rejected(writer, qdrant_server):
    origin, _, read_key = qdrant_server
    reader = index_client(origin, read_key)
    try:
        assert (await reader.count(writer.config.collection, exact=True)).count == 0
        with pytest.raises(UnexpectedResponse) as exc:
            await reader.delete_collection(writer.config.collection)
        assert exc.value.status_code in (401, 403)
        with pytest.raises(UnexpectedResponse) as exc:
            await reader.scroll(
                writer.config.collection,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="unindexed_secret", match=models.MatchValue(value="never")
                        )
                    ]
                ),
            )
        assert exc.value.status_code in (400, 403)
    finally:
        await reader.close()


async def test_missing_original_keeps_index_unavailable_during_repair(
    auth_client, object_store, database, services, writer, tmp_path
):
    client, _, _, h, _, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "missing-source")
    fixture_vectors(database, object_store, lease, accepted, tmp_path)
    await publisher.stage(scope(lease), lease)
    await delivery.run(await outbox.claim())
    assert await publisher.finalize(scope(lease), lease)
    with psycopg.connect(database["operator"]) as conn:
        key = conn.execute(
            "SELECT object_key FROM app.document_revisions WHERE id=%s", (lease.revision_id,)
        ).fetchone()[0]
    object_store.delete(key)
    reconciler = Reconciler(publisher.db, outbox, object_store, writer)
    assert (await reconciler.audit_workspace(lease.workspace_id)).repairs == 1
    with pytest.raises(Problem, match="storage_unavailable"):
        await delivery.run(await outbox.claim())
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT state FROM app.index_manifests WHERE revision_id=%s", (lease.revision_id,)
            ).fetchone()[0]
            == "unavailable"
        )

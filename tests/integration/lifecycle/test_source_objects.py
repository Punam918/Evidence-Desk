import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from evidencedesk.db import source_objects_repository as repo
from evidencedesk.errors import Problem
from evidencedesk.lifecycle.source_objects import SourceObjectPurger

from tests.integration.retrieval.test_retrieval import publish_fixture
from tests.integration.revisions.conftest import qdrant_server as qdrant_server
from tests.integration.revisions.conftest import writer as writer
from tests.integration.revisions.test_publication import services as services
from tests.security.uploads.test_uploads import setup

pytestmark = pytest.mark.integration


@pytest.fixture
async def source_objects(auth_client, database, object_store, services, tmp_path):
    client, _, _, h, base, url = setup(auth_client)
    lease, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path
    )
    prefix = f"workspaces/{lease.workspace_id}/documents/{artifact.document_id}/"
    data = b"Private uncommitted parser artifact"
    path = tmp_path / "blocks"
    path.write_bytes(data)
    orphan = prefix + f"revisions/{lease.revision_id}/blocks/uncommitted"
    object_store.put(orphan, path, "application/json", hashlib.sha256(data).hexdigest())
    yield SimpleNamespace(
        client=client,
        h=h,
        base=base,
        lease=lease,
        artifact=artifact,
        store=object_store,
        database=database,
        prefix=prefix,
        orphan=orphan,
        path=path,
        purger=SourceObjectPurger(services[2].db, object_store),
        outbox=services[1],
        delivery=services[3],
    )


def delete(h):
    response = h.client.delete(h.base + f"/knowledge-bases/{h.artifact.kb_id}", headers=h.h)
    assert response.status_code == 202, response.text


def receipt(h):
    with psycopg.connect(h.database["operator"]) as conn:
        return conn.execute(
            "SELECT purged_at FROM app.deletion_tombstones WHERE workspace_id=%s AND entity_type='document_objects' AND entity_id=%s",
            (h.lease.workspace_id, h.artifact.document_id),
        ).fetchone()


async def test_erases_whole_document_namespace_and_reconciles_after_workspace_cascade(
    source_objects,
):
    h = source_objects
    foreign = f"workspaces/{uuid4()}/documents/{uuid4()}/preserve"
    h.store.put(foreign, h.path, "text/plain", hashlib.sha256(h.path.read_bytes()).hexdigest())
    assert not (await h.purger.purge_workspace(h.lease.workspace_id)).selected
    delete(h)
    assert not (await h.purger.purge_workspace(uuid4())).selected
    page = await h.purger.purge_workspace(h.lease.workspace_id)
    assert page.selected and page.removed == 3 and page.verified_empty
    assert receipt(h)[0] is not None
    assert h.store.list_page(h.prefix)[0] == []
    assert h.store.list_page(foreign)[0]
    assert not (await h.purger.purge_workspace(h.lease.workspace_id)).selected
    # Derived point deletion still succeeds after originals/vector artifacts are gone.
    delivery = await h.outbox.claim()
    assert delivery and delivery.operation == "purge"
    await h.delivery.run(delivery)
    writer = h.delivery.writer
    assert (
        await writer.client.count(
            writer.config.collection, count_filter=writer.scope(h.artifact), exact=True
        )
    ).count == 0
    with psycopg.connect(h.database["operator"]) as conn:
        # Ownership anchors remain for Qdrant reconciliation until a separate purge.
        assert conn.execute(
            "SELECT count(*) FROM app.index_manifests WHERE workspace_id=%s",
            (h.lease.workspace_id,),
        ).fetchone() == (1,)
        conn.execute("DELETE FROM app.workspaces WHERE id=%s", (h.lease.workspace_id,))
        conn.execute(
            "UPDATE app.deletion_tombstones SET purged_at=now()-interval '2 hours' WHERE workspace_id=%s AND entity_type='document_objects'",
            (h.lease.workspace_id,),
        )
    h.store.put(h.orphan, h.path, "text/plain", hashlib.sha256(h.path.read_bytes()).hexdigest())
    page = await h.purger.purge_workspace(h.lease.workspace_id)
    assert page.removed == 1 and page.verified_empty
    assert h.store.list_page(foreign)[0]


async def test_bounded_pages_and_storage_failure_keep_durable_retry_authority(
    source_objects, monkeypatch
):
    h = source_objects
    for n in range(103):
        h.store.client.put_object(
            Bucket=h.store.bucket, Key=h.prefix + f"staging/{n}", Body=b"Private staging"
        )
    delete(h)
    original = h.store.delete
    calls = 0

    def fail(key):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise Problem(503, "storage_unavailable", "Synthetic storage interruption", True)
        original(key)

    with monkeypatch.context() as patch:
        patch.setattr(h.store, "delete", fail)
        with pytest.raises(Problem, match="storage_unavailable"):
            await h.purger.purge_workspace(h.lease.workspace_id)
    assert receipt(h) == (None,)
    first = await h.purger.purge_workspace(h.lease.workspace_id)
    assert first.removed == 100 and not first.verified_empty
    assert receipt(h) == (None,)
    second = await h.purger.purge_workspace(h.lease.workspace_id)
    assert second.removed == 5 and second.verified_empty


async def test_receipt_failure_does_not_claim_erasure_and_empty_retry_succeeds(
    source_objects, monkeypatch
):
    h = source_objects
    delete(h)
    original = repo.receipt

    async def fail(*args):
        await original(*args)
        raise RuntimeError("Synthetic receipt failure")

    with monkeypatch.context() as patch:
        patch.setattr(repo, "receipt", fail)
        with pytest.raises(RuntimeError):
            await h.purger.purge_workspace(h.lease.workspace_id)
    assert receipt(h) == (None,)
    page = await h.purger.purge_workspace(h.lease.workspace_id)
    assert page.removed == 0 and page.verified_empty


async def test_live_document_overrides_inconsistent_object_receipt(source_objects):
    h = source_objects
    with psycopg.connect(h.database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.deletion_tombstones(workspace_id,id,entity_type,entity_id) VALUES(%s,%s,'document_objects',%s)",
            (h.lease.workspace_id, uuid4(), h.artifact.document_id),
        )
    with pytest.raises(Problem, match="purge_not_authorized"):
        await h.purger.purge_workspace(h.lease.workspace_id)
    assert len(h.store.list_page(h.prefix)[0]) == 3


@pytest.mark.parametrize("status", ["Enabled", "Suspended"])
async def test_versioned_bucket_is_refused_before_any_erasure(source_objects, status):
    h = source_objects
    delete(h)
    h.store.client.put_bucket_versioning(
        Bucket=h.store.bucket, VersioningConfiguration={"Status": status}
    )
    with pytest.raises(Problem, match="versioned_storage_unsupported"):
        await h.purger.purge_workspace(h.lease.workspace_id)
    assert receipt(h) is None
    assert len(h.store.list_page(h.prefix)[0]) == 3


async def test_actual_object_purge_cli_needs_only_database_and_storage(source_objects, tmp_path):
    h = source_objects
    delete(h)
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("ED_", "LANGCHAIN_", "LANGSMITH_"))
    }
    access, secret = tmp_path / "access", tmp_path / "secret"
    access.write_text("testing")
    secret.write_text("testing")
    env.update(
        ED_DATABASE_URL=h.database["ed_app"],
        ED_S3_ENDPOINT=h.store.client.meta.endpoint_url,
        ED_S3_BUCKET=h.store.bucket,
        ED_S3_ACCESS_KEY_FILE=str(access),
        ED_S3_SECRET_KEY_FILE=str(secret),
    )
    result = subprocess.run(
        [
            str(Path(sys.executable).with_name("evidencedesk")),
            "source-objects-purge",
            "--workspace",
            str(h.lease.workspace_id),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "event": "source_objects",
        "selected": True,
        "removed": 3,
        "verified_empty": True,
    }
    assert "Private" not in result.stdout + result.stderr

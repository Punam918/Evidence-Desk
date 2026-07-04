import hashlib
from uuid import uuid4

import psycopg
import pytest
from evidencedesk.errors import Problem
from evidencedesk.evidence.service import EvidenceService
from evidencedesk.storage.download import prepare

from tests.integration.retrieval.test_retrieval import publish_fixture
from tests.integration.revisions.test_publication import scope
from tests.security.uploads.test_uploads import setup

pytestmark = pytest.mark.integration


async def seeded(auth_client, object_store, database, services, tmp_path, data=b"Original source"):
    client, state, identity, h, base, url = setup(auth_client)
    lease, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path, data=data
    )
    return client, h, base, lease, artifact


async def test_exact_passage_and_superseded_original(
    auth_client, object_store, database, services, tmp_path
):
    client, h, base, lease, artifact = await seeded(
        auth_client, object_store, database, services, tmp_path
    )
    chunk = artifact.points[0]
    result = client.get(f"{base}/chunks/{chunk.id}")
    assert result.status_code == 200, result.text
    assert result.json() == {
        "id": str(chunk.id),
        "document_id": str(artifact.document_id),
        "revision_id": str(lease.revision_id),
        "text": "Fixture evidence passage 0.",
        "page_start": None,
        "page_end": None,
        "heading_path": [],
        "content_sha256": hashlib.sha256(b"Fixture evidence passage 0.").hexdigest(),
    }
    newer, _ = await publish_fixture(
        client,
        f"{base}/documents/{artifact.document_id}/revisions",
        h,
        database,
        object_store,
        services,
        tmp_path,
        data=b"Replacement source",
    )
    assert client.get(f"{base}/chunks/{chunk.id}").json() == result.json()
    route = f"{base}/documents/{artifact.document_id}/download"
    assert client.get(route).content == b"Replacement source"
    original = client.get(route, params={"revision_id": str(lease.revision_id)})
    assert original.content == b"Original source"
    assert original.headers["content-type"] == "application/octet-stream"
    assert original.headers["content-disposition"].startswith("attachment;")
    assert original.headers["cache-control"] == "private, no-store"
    assert original.headers["x-content-type-options"] == "nosniff"
    assert client.get(route, params={"revision_id": str(uuid4())}).status_code == 404
    foreign = client.post(
        "/v1/workspaces", json={"name": "Other"}, headers={**h, "Idempotency-Key": str(uuid4())}
    ).json()["id"]
    assert client.get(f"/v1/workspaces/{foreign}/chunks/{chunk.id}").status_code == 404
    assert (
        client.get(
            f"/v1/workspaces/{foreign}/documents/{artifact.document_id}/download"
        ).status_code
        == 404
    )


@pytest.mark.parametrize("removed", ["document", "kb", "member", "workspace", "staging"])
async def test_source_reads_fail_closed(
    auth_client, object_store, database, services, tmp_path, removed
):
    client, _, base, lease, artifact = await seeded(
        auth_client, object_store, database, services, tmp_path
    )
    with psycopg.connect(database["operator"]) as conn:
        if removed == "member":
            conn.execute(
                "DELETE FROM app.memberships WHERE workspace_id=%s AND user_id=%s",
                (lease.workspace_id, lease.actor_id),
            )
        elif removed == "staging":
            conn.execute(
                "UPDATE app.document_revisions SET status='verifying' WHERE id=%s",
                (lease.revision_id,),
            )
        else:
            table, uid = {
                "document": ("documents", artifact.document_id),
                "kb": ("knowledge_bases", artifact.kb_id),
                "workspace": ("workspaces", lease.workspace_id),
            }[removed]
            conn.execute(f"UPDATE app.{table} SET deleted_at=now() WHERE id=%s", (uid,))
    for path in [
        f"{base}/chunks/{artifact.points[0].id}",
        f"{base}/documents/{artifact.document_id}/download",
    ]:
        response = client.get(path)
        assert response.status_code == 404, response.text
        assert "Fixture evidence" not in response.text and "Original source" not in response.text


@pytest.mark.parametrize("fault", ["missing", "corrupt", "unsafe_name"])
async def test_attachment_integrity_and_safe_headers(
    auth_client, object_store, database, services, tmp_path, fault
):
    payload = b"<script>window.pwned=true</script>"
    client, _, base, lease, artifact = await seeded(
        auth_client, object_store, database, services, tmp_path, payload
    )
    with psycopg.connect(database["operator"]) as conn:
        key = conn.execute(
            "SELECT object_key FROM app.document_revisions WHERE id=%s", (lease.revision_id,)
        ).fetchone()[0]
        if fault == "unsafe_name":
            conn.execute(
                "UPDATE app.documents SET title=%s WHERE id=%s",
                ('../../evil"\r\nX-Injected: yes\u202e.html', artifact.document_id),
            )
    if fault == "missing":
        object_store.delete(key)
    if fault == "corrupt":
        object_store.client.put_object(Bucket=object_store.bucket, Key=key, Body=b"corrupt")
    response = client.get(f"{base}/documents/{artifact.document_id}/download")
    if fault == "unsafe_name":
        assert response.status_code == 200, response.text
        assert response.content == payload
        assert response.headers["content-type"] == "application/octet-stream"
        assert "x-injected" not in response.headers
        assert "\r" not in response.headers["content-disposition"]
        assert "%E2%80%AE" not in response.headers["content-disposition"]
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    else:
        assert response.status_code in (422, 503), response.text
        assert payload not in response.content


async def test_revoke_during_copy_rechecks_before_response(
    auth_client, object_store, database, services, tmp_path, monkeypatch
):
    client, _, base, lease, artifact = await seeded(
        auth_client, object_store, database, services, tmp_path
    )
    original = object_store.download

    def revoked(*args):
        original(*args)
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.documents SET deleted_at=now() WHERE id=%s", (artifact.document_id,)
            )

    monkeypatch.setattr(object_store, "download", revoked)
    response = client.get(f"{base}/documents/{artifact.document_id}/download")
    assert response.status_code == 404
    assert b"Original source" not in response.content


async def test_attachment_rechecks_between_chunks_and_cleans_tempfile(
    auth_client, object_store, database, services, tmp_path
):
    client, _, base, lease, artifact = await seeded(
        auth_client, object_store, database, services, tmp_path, b"x" * 140000
    )
    attachment = await prepare(
        EvidenceService(services[2].db),
        object_store,
        scope(lease),
        artifact.document_id,
        lease.revision_id,
    )
    stream = attachment.stream()
    assert await anext(stream) == b"x" * 65536
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "DELETE FROM app.memberships WHERE workspace_id=%s AND user_id=%s",
            (lease.workspace_id, lease.actor_id),
        )
    with pytest.raises(Problem, match="not_found"):
        await anext(stream)
    assert not attachment.path.exists()

from uuid import uuid4

import psycopg
import pytest

from tests.support import headers, sign_in

pytestmark = pytest.mark.integration


def test_kb_idempotency_duplicates_and_cursor_scope(auth_client):
    client, state = auth_client
    identity = sign_in(client, state)
    h = headers(identity)
    workspaces = [
        client.post(
            "/v1/workspaces",
            json={"name": f"Knowledge {i}"},
            headers={**h, "Idempotency-Key": uuid4().hex},
        ).json()["id"]
        for i in range(2)
    ]
    base = f"/v1/workspaces/{workspaces[0]}/knowledge-bases"
    first = client.post(base, json={"name": " Handbook "}, headers=h)
    assert first.status_code == 201, first.text
    assert first.json()["name"] == "Handbook"
    assert client.post(base, json={"name": " Handbook "}, headers=h).json() == first.json()
    assert client.post(base, json={"name": "Other"}, headers=h).status_code == 409
    assert (
        client.post(
            base, json={"name": "HANDBOOK"}, headers={**h, "Idempotency-Key": uuid4().hex}
        ).status_code
        == 409
    )
    for i in range(3):
        assert (
            client.post(
                base, json={"name": f"Extra {i}"}, headers={**h, "Idempotency-Key": uuid4().hex}
            ).status_code
            == 201
        )
    page = client.get(base, params={"limit": 2}).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    later = client.get(base, params={"cursor": page["next_cursor"]}).json()
    assert len(later["items"]) == 2 and not {row["id"] for row in page["items"]} & {
        row["id"] for row in later["items"]
    }
    assert (
        client.get(
            f"/v1/workspaces/{workspaces[1]}/knowledge-bases",
            params={"cursor": page["next_cursor"]},
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/v1/workspaces/{workspaces[1]}/knowledge-bases/{first.json()['id']}/documents"
        ).status_code
        == 404
    )


def seed_document(database, workspace, kb, actor):
    document, previous, latest = uuid4(), uuid4(), uuid4()
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.documents(workspace_id,id,kb_id,title,created_by,active_revision_id,latest_revision_id) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (workspace, document, kb, "Policy", actor, previous, latest),
        )
        for n, revision, status in [(1, previous, "published"), (2, latest, "failed")]:
            conn.execute(
                "INSERT INTO app.document_revisions(workspace_id,id,document_id,kb_id,revision_no,status,object_key,mime_type,byte_size,content_sha256,parser_version,chunk_profile_id,profile_hash,embedding_profile_id,chunk_count) VALUES(%s,%s,%s,%s,%s,%s,%s,'text/plain',10,%s,'v1','v1','fixture','bge_small_en_v15',1)",
                (workspace, revision, document, kb, n, status, f"private/{revision}", "a" * 64),
            )
    return document, previous, latest


def test_document_metadata_and_tombstone_hides_all_children(auth_client, database):
    client, state = auth_client
    identity = sign_in(client, state)
    h = headers(identity)
    workspace = client.post("/v1/workspaces", json={"name": "Sources"}, headers=h).json()["id"]
    base = f"/v1/workspaces/{workspace}"
    kb = client.post(base + "/knowledge-bases", json={"name": "Handbook"}, headers=h).json()["id"]
    document, previous, latest = seed_document(database, workspace, kb, identity["user"]["id"])
    listing = client.get(base + f"/knowledge-bases/{kb}/documents").json()["items"]
    assert len(listing) == 1
    assert (
        listing[0]["active_revision_id"] == str(previous)
        and listing[0]["latest_revision_id"] == str(latest)
        and listing[0]["status"] == "failed"
    )
    assert client.get(base + "/knowledge-bases").json()["items"][0]["document_count"] == 1
    revisions = client.get(base + f"/documents/{document}/revisions", params={"limit": 1}).json()
    assert revisions["items"][0]["revision_no"] == 2 and "object_key" not in revisions["items"][0]
    assert (
        client.get(
            base + f"/documents/{document}/revisions", params={"cursor": revisions["next_cursor"]}
        ).json()["items"][0]["revision_no"]
        == 1
    )
    deleted = client.delete(base + f"/knowledge-bases/{kb}", headers=h)
    assert deleted.status_code == 202, deleted.text
    assert client.delete(base + f"/knowledge-bases/{kb}", headers=h).json() == deleted.json()
    assert client.get(base + "/knowledge-bases").json()["items"] == []
    assert client.get(base + f"/knowledge-bases/{kb}/documents").status_code == 404
    assert client.get(base + f"/documents/{document}").status_code == 404
    assert client.get(base + f"/documents/{document}/revisions").status_code == 404
    assert (
        client.post(base + "/knowledge-bases", json={"name": "Handbook"}, headers=h).status_code
        == 404
    )
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT deleted_at IS NOT NULL FROM app.documents WHERE workspace_id=%s AND id=%s",
            (workspace, document),
        ).fetchone()[0]
        assert (
            conn.execute(
                "SELECT count(*) FROM app.deletion_tombstones WHERE workspace_id=%s AND entity_type='knowledge_base'",
                (workspace,),
            ).fetchone()[0]
            == 1
        )
        manifest = conn.execute(
            "SELECT object_manifest FROM app.deletion_tombstones WHERE workspace_id=%s",
            (workspace,),
        ).fetchone()[0]
        assert len(manifest) == 2 and all("key" in row and "sha256" in row for row in manifest)


def test_roles_and_foreign_objects_never_expose_shared_documents(auth_client, database):
    client, state = auth_client
    owner = sign_in(client, state)
    h = headers(owner)
    workspace = client.post("/v1/workspaces", json={"name": "Roles"}, headers=h).json()["id"]
    base = f"/v1/workspaces/{workspace}"
    kb = client.post(base + "/knowledge-bases", json={"name": "Shared"}, headers=h).json()["id"]
    document, _, _ = seed_document(database, workspace, kb, owner["user"]["id"])
    state["subject"] = str(uuid4())
    viewer = sign_in(client, state)
    assert client.get(base + f"/documents/{document}").status_code == 404
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.memberships(workspace_id,user_id,role) VALUES(%s,%s,'viewer')",
            (workspace, viewer["user"]["id"]),
        )
    assert client.get(base + f"/documents/{document}").status_code == 200
    assert (
        client.post(
            base + "/knowledge-bases", json={"name": "Not allowed"}, headers=headers(viewer)
        ).status_code
        == 403
    )
    assert (
        client.delete(base + f"/knowledge-bases/{kb}", headers=headers(viewer)).status_code == 403
    )
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.memberships SET role='editor' WHERE workspace_id=%s AND user_id=%s",
            (workspace, viewer["user"]["id"]),
        )
    assert (
        client.post(
            base + "/knowledge-bases", json={"name": "Editor collection"}, headers=headers(viewer)
        ).status_code
        == 201
    )
    assert (
        client.delete(base + f"/knowledge-bases/{kb}", headers=headers(viewer)).status_code == 403
    )


async def test_delete_invalidates_running_ingestion_lease(auth_client, database):
    from uuid import UUID

    from evidencedesk.db.jobs_repository import JobRepository
    from evidencedesk.db.session import AuthContext, authorized_transaction, engine
    from evidencedesk.errors import LeaseLost

    client, state = auth_client
    identity = sign_in(client, state)
    h = headers(identity)
    workspace = client.post("/v1/workspaces", json={"name": "Lease fence"}, headers=h).json()["id"]
    base = f"/v1/workspaces/{workspace}"
    kb = client.post(base + "/knowledge-bases", json={"name": "Ingesting"}, headers=h).json()["id"]
    _, _, revision = seed_document(database, workspace, kb, identity["user"]["id"])
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.document_revisions SET status='embedding' WHERE workspace_id=%s AND id=%s",
            (workspace, revision),
        )
        conn.execute(
            "INSERT INTO app.jobs(workspace_id,id,kind,status,revision_id,requested_by,dedupe_key) VALUES(%s,%s,'ingest','queued',%s,%s,%s)",
            (workspace, uuid4(), revision, identity["user"]["id"], f"ingest:{revision}"),
        )
    db, dispatch = engine(database["ed_app"]), engine(database["ed_dispatch"])
    try:
        lease = await JobRepository(dispatch).claim("ingest", "knowledge-delete-fixture")
        assert lease
        assert client.delete(base + f"/knowledge-bases/{kb}", headers=h).status_code == 202
        ctx = AuthContext(UUID(identity["user"]["id"]), UUID(workspace), "owner", 1, uuid4())
        with pytest.raises(LeaseLost):
            async with authorized_transaction(db, ctx) as conn:
                await JobRepository.fence(conn, lease)
    finally:
        await db.dispose()
        await dispatch.dispose()

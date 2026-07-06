import asyncio
import hashlib
import io
import zipfile
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem

from tests.support import headers, sign_in

pytestmark = pytest.mark.integration


def setup(auth_client):
    client, state = auth_client
    identity = sign_in(client, state)
    h = headers(identity)
    workspace = client.post("/v1/workspaces", json={"name": "Upload tests"}, headers=h).json()["id"]
    base = f"/v1/workspaces/{workspace}"
    kb = client.post(base + "/knowledge-bases", json={"name": "Sources"}, headers=h).json()["id"]
    return client, state, identity, h, base, base + f"/knowledge-bases/{kb}/documents"


def upload(client, url, h, data=b"A safe document.", name="source.txt", mime="text/plain"):
    return client.post(url, files={"file": (name, data, mime)}, headers=h)


def objects(store):
    return store.list_page("workspaces/")[0]


def test_private_immutable_object_idempotency_and_checksum(
    auth_client, object_store, database, tmp_path
):
    client, _, _, h, base, url = setup(auth_client)
    first = upload(client, url, h, name="../../source.txt")
    assert first.status_code == 202, first.text
    result = first.json()
    assert result["document"]["title"] == "source.txt"
    assert result["document"]["active_revision_id"] is None
    assert result["document"]["status"] == "queued"
    assert upload(client, url, h, name="../../source.txt").json() == result
    assert upload(client, url, h, data=b"Changed").status_code == 409
    stored = objects(object_store)
    assert len(stored) == 1
    key = stored[0].key
    assert "source.txt" not in key and result["revision_id"] in key
    assert key.endswith(hashlib.sha256(b"A safe document.").hexdigest())
    public = httpx.get(f"{object_store.client.meta.endpoint_url}/{object_store.bucket}/{key}")
    assert public.status_code == 403
    path = tmp_path / "download"
    object_store.download(key, path, key.rsplit("/", 1)[1])
    assert path.read_bytes() == b"A safe document."
    object_store.put(key, path, "text/plain", hashlib.sha256(path.read_bytes()).hexdigest())
    path.write_bytes(b"different")
    with pytest.raises(Problem, match="checksum"):
        object_store.put(key, path, "text/plain", hashlib.sha256(path.read_bytes()).hexdigest())
    with psycopg.connect(database["operator"]) as conn:
        revision = conn.execute(
            "SELECT content_sha256,byte_size FROM app.document_revisions WHERE id=%s",
            (result["revision_id"],),
        ).fetchone()
        assert revision == (hashlib.sha256(b"A safe document.").hexdigest(), 16)
        assert (
            conn.execute(
                "SELECT count(*) FROM app.jobs WHERE revision_id=%s", (result["revision_id"],)
            ).fetchone()[0]
            == 1
        )
    assert client.get(base + f"/documents/{result['document']['id']}/revisions").status_code == 200


def test_revisions_reprocess_and_failed_replacement_keep_active(
    auth_client, object_store, database
):
    client, _, _, h, base, url = setup(auth_client)
    original = upload(client, url, h).json()
    doc = original["document"]["id"]
    revisions = base + f"/documents/{doc}/revisions"
    assert upload(client, revisions, h, b"new version").status_code == 409
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.document_revisions SET status='published' WHERE id=%s",
            (original["revision_id"],),
        )
        conn.execute(
            "UPDATE app.documents SET active_revision_id=%s WHERE id=%s",
            (original["revision_id"], doc),
        )
        conn.execute(
            "UPDATE app.jobs SET status='succeeded' WHERE revision_id=%s",
            (original["revision_id"],),
        )
    dedupe = upload(client, revisions, h)
    assert dedupe.status_code == 202, dedupe.text
    assert dedupe.json()["revision_id"] == original["revision_id"]
    assert dedupe.json()["job"]["status"] == "succeeded"
    reprocess = base + f"/documents/{doc}/reprocess"
    assert (
        client.post(
            reprocess, json={"profile_id": "structure_v1", "expected_version": 1}, headers=h
        ).json()["revision_id"]
        == original["revision_id"]
    )
    second = upload(client, revisions, {**h, "Idempotency-Key": uuid4().hex}, b"new version").json()
    assert second["document"]["version"] == 2
    assert second["document"]["active_revision_id"] == original["revision_id"]
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.document_revisions SET status='failed' WHERE id=%s",
            (second["revision_id"],),
        )
        conn.execute(
            "UPDATE app.jobs SET status='dead' WHERE revision_id=%s", (second["revision_id"],)
        )
    assert (
        client.post(
            reprocess,
            json={"profile_id": "structure_v1", "expected_version": 1},
            headers={**h, "Idempotency-Key": uuid4().hex},
        ).status_code
        == 409
    )
    result = client.post(
        reprocess,
        json={"profile_id": "structure_v1", "expected_version": 2},
        headers={**h, "Idempotency-Key": uuid4().hex},
    )
    assert result.status_code == 202, result.text
    assert len(objects(object_store)) == 2  # reprocess shares existing immutable original
    listing = client.get(revisions).json()["items"]
    assert [r["revision_no"] for r in listing] == [3, 2, 1]
    assert result.json()["document"]["active_revision_id"] == original["revision_id"]


@pytest.mark.parametrize(
    "name,data,mime",
    [
        ("bad.exe", b"MZbinary", "application/octet-stream"),
        ("bad.txt", b"%PDF-1.7", "text/plain"),
        ("bad.pdf", b"not a PDF", "application/pdf"),
        ("bad.txt", b"invalid\xff", "text/plain"),
        ("bad.txt", b"plain text", "application/pdf"),
        ("empty.txt", b"", "text/plain"),
        ("nul.txt", b"a" * 2048 + b"\0", "text/plain"),
    ],
)
def test_invalid_upload_never_reaches_storage(auth_client, object_store, name, data, mime):
    client, _, _, h, _, url = setup(auth_client)
    response = upload(client, url, h, data, name, mime)
    assert response.status_code in (413, 422), response.text
    assert not objects(object_store)


def test_multipart_and_exact_size_boundaries(auth_client, object_store):
    client, _, _, h, _, url = setup(auth_client)
    assert upload(client, url, h, b"a" * (20 * 1024 * 1024 + 1)).status_code == 413
    assert upload(client, url, h, b"a" * (20 * 1024 * 1024)).status_code == 202
    assert (
        client.post(
            url, files=[("file", ("a.txt", b"abc")), ("file", ("b.txt", b"abc"))], headers=h
        ).status_code
        == 422
    )
    assert client.post(url, files={"other": ("a.txt", b"abc")}, headers=h).status_code == 422
    assert (
        client.post(
            url, files={"file": ("a.txt", b"abc")}, data={"url": "https://example.test"}, headers=h
        ).status_code
        == 422
    )
    # No Content-Length: the receive stream enforces the total even for a file without a closing boundary.
    prefix = b'--ed\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'
    response = client.post(
        url,
        content=iter([prefix] + [b"a" * 65536] * 322),
        headers={**h, "Content-Type": "multipart/form-data; boundary=ed"},
    )
    assert response.status_code == 413, response.text
    assert len(objects(object_store)) == 1


def test_zip_paths_expansion_macros_and_directory_limits(auth_client, object_store):
    client, _, _, h, _, url = setup(auth_client)
    for entries in [
        {"../evil": b"x"},
        {"word/vbaProject.bin": b"x"},
        {"word/big.xml": b"0" * 100000},
        {str(i): b"x" for i in range(1001)},
    ]:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in {
                "[Content_Types].xml": b"<Types/>",
                "word/document.xml": b"<document/>",
                **entries,
            }.items():
                archive.writestr(name, data)
        response = upload(
            client,
            url,
            h,
            stream.getvalue(),
            "bad.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        assert response.status_code == 422, response.text
    assert not objects(object_store)


def test_live_role_checked_before_body_and_replay(auth_client, object_store, database):
    client, state, identity, h, base, url = setup(auth_client)
    first = upload(client, url, h)
    assert first.status_code == 202
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.memberships SET role='viewer' WHERE workspace_id=%s AND user_id=%s",
            (base.split("/")[-1], identity["user"]["id"]),
        )
    assert upload(client, url, h).status_code == 403
    assert client.post(url, content=b"malformed", headers=h).status_code == 403
    state["subject"] = str(uuid4())
    foreign = sign_in(client, state)
    assert upload(client, url, headers(foreign)).status_code == 404
    assert len(objects(object_store)) == 1


def test_quota_race_creates_only_one_revision(auth_client, object_store, monkeypatch, tmp_path):
    from evidencedesk.db import upload_repository
    from evidencedesk.ingestion.models import UploadFileInfo

    client, _, identity, _, base, url = setup(auth_client)
    monkeypatch.setattr(upload_repository, "ORIGINAL_QUOTA", 20)
    ctx = AuthContext(UUID(identity["user"]["id"]), UUID(base.split("/")[-1]), "owner", 1, uuid4())
    path = tmp_path / "source"
    path.write_bytes(b"A safe document.")
    info = UploadFileInfo(
        path, "source.txt", "text/plain", 16, hashlib.sha256(path.read_bytes()).hexdigest()
    )

    # Exercise the domain race independently of the per-user HTTP upload slot.
    async def race():
        return await asyncio.gather(
            *(
                client.app.state.uploads.upload(
                    ctx, UUID(url.split("/")[-2]), None, info, uuid4().hex
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )

    results = client.portal.call(race)
    assert sum(isinstance(r, Problem) and r.code == "storage_quota_exceeded" for r in results) == 1
    assert sum(not isinstance(r, BaseException) for r in results) == 1
    assert len(objects(object_store)) == 1


def test_db_failure_cleans_orphan_and_cleanup_preserves_committed(
    auth_client, object_store, monkeypatch
):
    from evidencedesk.db import upload_repository

    client, _, identity, h, base, url = setup(auth_client)
    original_create = upload_repository.create

    async def fail(*args, **kwargs):
        await original_create(*args, **kwargs)
        raise RuntimeError("Injected failure before commit")

    monkeypatch.setattr(upload_repository, "create", fail)
    assert upload(client, url, h).status_code == 500
    assert not objects(object_store)
    monkeypatch.setattr(upload_repository, "create", original_create)
    assert upload(client, url, h).status_code == 202
    ctx = AuthContext(UUID(identity["user"]["id"]), UUID(base.split("/")[-1]), "owner", 1, uuid4())
    assert (
        client.portal.call(
            client.app.state.uploads.remove_unreferenced, ctx, objects(object_store)[0].key
        )
        is False
    )
    assert len(objects(object_store)) == 1


def test_age_gated_sweep_removes_only_unreferenced_originals(
    auth_client, object_store, tmp_path, monkeypatch
):
    from evidencedesk.ingestion import upload as module

    client, _, identity, h, base, url = setup(auth_client)
    assert upload(client, url, h).status_code == 202
    ctx = AuthContext(UUID(identity["user"]["id"]), UUID(base.split("/")[-1]), "owner", 1, uuid4())
    orphan = (
        f"workspaces/{ctx.workspace_id}/documents/{uuid4()}/revisions/{uuid4()}/original/"
        + "a" * 64
    )
    path = tmp_path / "orphan"
    path.write_bytes(b"orphan")
    object_store.put(orphan, path, "text/plain", hashlib.sha256(b"orphan").hexdigest())
    assert client.portal.call(client.app.state.uploads.sweep_orphans, ctx) == 0

    class FutureClock:
        @staticmethod
        def now(tz):
            return datetime.now(UTC) + timedelta(hours=25)

    monkeypatch.setattr(module, "datetime", FutureClock)
    assert client.portal.call(client.app.state.uploads.sweep_orphans, ctx) == 1
    assert len(objects(object_store)) == 1


def test_commit_acknowledgement_loss_preserves_object_and_replay(
    auth_client, object_store, monkeypatch
):
    from contextlib import asynccontextmanager

    from evidencedesk.ingestion import upload as module

    client, _, _, h, _, url = setup(auth_client)
    original = module.authorized_transaction
    transactions = 0

    @asynccontextmanager
    async def lose_ack(*args, **kwargs):
        nonlocal transactions
        transactions += 1
        number = transactions
        async with original(*args, **kwargs) as conn:
            yield conn
        if number == 3:  # preflight, pre-PUT admission, then committed revision
            raise RuntimeError("Injected connection loss after successful COMMIT")

    monkeypatch.setattr(module, "authorized_transaction", lose_ack)
    assert upload(client, url, h).status_code == 500
    assert len(objects(object_store)) == 1
    response = upload(client, url, h)
    assert response.status_code == 202, response.text
    assert len(objects(object_store)) == 1


def test_interrupted_body_leaves_no_object(auth_client, object_store):
    client, _, _, h, _, url = setup(auth_client)

    def interrupted():
        yield b'--ed\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\npartial'
        raise OSError("Client disconnected")

    response = client.post(
        url,
        content=interrupted(),
        headers={**h, "Content-Type": "multipart/form-data; boundary=ed"},
    )
    assert response.status_code >= 400
    assert not objects(object_store)


def test_actor_upload_slot_rejects_parallel_body(auth_client, object_store, database):
    from evidencedesk.db.upload_slot import UploadSlot

    client, _, identity, h, _, url = setup(auth_client)
    slot = client.portal.call(UploadSlot.acquire, database["ed_app"], UUID(identity["user"]["id"]))
    try:
        response = upload(client, url, h)
        assert response.status_code == 429 and response.json()["code"] == "upload_concurrency"
        assert not objects(object_store)
    finally:
        client.portal.call(slot.close)
    assert upload(client, url, h).status_code == 202


def test_full_tenant_queue_rejects_before_body_but_allows_idempotent_replay(
    auth_client, object_store, database
):
    client, _, identity, h, base, url = setup(auth_client)
    accepted = upload(client, url, h).json()
    with psycopg.connect(database["operator"]) as conn:
        for _ in range(9):
            conn.execute(
                "INSERT INTO app.jobs(workspace_id,id,kind,status,revision_id,requested_by,dedupe_key) VALUES(%s,%s,'ingest','queued',%s,%s,%s)",
                (
                    base.split("/")[-1],
                    uuid4(),
                    accepted["revision_id"],
                    identity["user"]["id"],
                    uuid4().hex,
                ),
            )
    response = client.post(
        url, content=b"invalid body", headers={**h, "Idempotency-Key": uuid4().hex}
    )
    assert response.status_code == 429 and response.json()["code"] == "ingest_queue_full"
    assert upload(client, url, h).json() == accepted
    assert len(objects(object_store)) == 1


def test_global_queue_counts_other_tenants(auth_client, object_store, database):
    client, _, _, target_headers, _, target_url = setup(auth_client)
    _, _, actor, other_headers, other_base, other_url = setup(auth_client)
    accepted = upload(client, other_url, other_headers).json()
    with psycopg.connect(database["operator"]) as conn:
        for _ in range(99):
            conn.execute(
                "INSERT INTO app.jobs(workspace_id,id,kind,status,revision_id,requested_by,dedupe_key) VALUES(%s,%s,'ingest','queued',%s,%s,%s)",
                (
                    other_base.split("/")[-1],
                    uuid4(),
                    accepted["revision_id"],
                    actor["user"]["id"],
                    uuid4().hex,
                ),
            )
    # The caller has no jobs in the target workspace; the global bound still rejects admission.
    response = client.post(
        target_url, content=b"not read", headers={**other_headers, "Idempotency-Key": uuid4().hex}
    )
    assert response.status_code == 429 and response.json()["code"] == "ingest_queue_full"

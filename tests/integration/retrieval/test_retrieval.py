from dataclasses import replace
from uuid import UUID, uuid4

import psycopg
import pytest
from evidencedesk.db.session import AuthContext
from evidencedesk.embeddings.encoder import Encoder
from evidencedesk.embeddings.models import SparseVector
from evidencedesk.errors import Problem
from evidencedesk.index.qdrant import client as index_client
from evidencedesk.retrieval.index import SearchIndex
from evidencedesk.retrieval.service import Retriever

from tests.integration.revisions.test_publication import fixture_vectors, scope
from tests.security.uploads.test_uploads import setup, upload

pytestmark = pytest.mark.integration


class SyntheticEncoder:
    QUERY_INSTRUCTION = Encoder.QUERY_INSTRUCTION

    def query_dense(self, text):
        return [1.0] + [0.0] * 383

    def query_sparse(self, text):
        return SparseVector(indices=[1], values=[1.0])


@pytest.fixture
async def retriever(services, qdrant_server):
    _, _, publisher, _ = services
    reader = index_client(qdrant_server[0], qdrant_server[2])
    try:
        yield Retriever(publisher.db, SearchIndex(reader), SyntheticEncoder())
    finally:
        await reader.close()


async def publish_fixture(
    client, url, h, database, object_store, services, tmp_path, *, data=b"Source", n=3, texts=None
):
    response = upload(client, url, {**h, "Idempotency-Key": str(uuid4())}, data)
    assert response.status_code == 202, response.text
    accepted = response.json()
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "retrieval-fixture")
    artifact = fixture_vectors(database, object_store, lease, accepted, tmp_path, n, texts=texts)
    await publisher.stage(scope(lease), lease)
    await delivery.run(await outbox.claim())
    assert await publisher.finalize(scope(lease), lease)
    return lease, artifact


async def test_every_branch_filters_tenant_scope_and_hydrates_database_text(
    auth_client, object_store, database, services, writer, retriever, tmp_path, monkeypatch
):
    client, _, _, h, base, url = setup(auth_client)
    lease, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path
    )
    other = client.post(
        "/v1/workspaces",
        json={"name": "Foreign workspace"},
        headers={**h, "Idempotency-Key": str(uuid4())},
    ).json()["id"]
    kb = client.post(
        f"/v1/workspaces/{other}/knowledge-bases",
        json={"name": "Foreign source"},
        headers={**h, "Idempotency-Key": str(uuid4())},
    ).json()["id"]
    foreign_lease, foreign = await publish_fixture(
        client,
        f"/v1/workspaces/{other}/knowledge-bases/{kb}/documents",
        h,
        database,
        object_store,
        services,
        tmp_path,
        data=b"Sensitive foreign source",
    )
    original = retriever.index.client.query_batch_points
    observed = []

    async def spy(collection, requests, **kwargs):
        for request in requests:
            conditions = {c.key: c.match for c in request.filter.must}
            assert conditions["workspace_id"].value == str(lease.workspace_id)
            assert conditions["kb_id"].any == [str(artifact.kb_id)]
            assert conditions["revision_id"].any == [str(lease.revision_id)]
            assert conditions["profile_id"].value == artifact.profile_id
            assert request.params.exact is True
            assert request.with_vector is False
        observed.extend(requests)
        return await original(collection, requests, **kwargs)

    monkeypatch.setattr(retriever.index.client, "query_batch_points", spy)
    result = await retriever.retrieve(scope(lease), "Fixture evidence", [artifact.kb_id])
    assert len(observed) == 2
    assert result.evidence[0].passage.chunk_id == artifact.points[0].id
    assert all(e.passage.revision_id == lease.revision_id for e in result.evidence)
    assert result.evidence[0].passage.text == "Fixture evidence passage 0."
    assert not result.degraded
    bad = AuthContext(uuid4(), foreign_lease.workspace_id, "owner", 1, uuid4())
    with pytest.raises(Problem, match="not_found"):
        await retriever.retrieve(bad, "secret", [foreign.kb_id])
    with pytest.raises(Problem, match="not_found"):
        await retriever.retrieve(scope(lease), "secret", [foreign.kb_id])


async def test_unpublished_and_superseded_are_excluded_from_ask_but_compare_is_explicit(
    auth_client, object_store, database, services, writer, retriever, tmp_path
):
    client, _, _, h, base, url = setup(auth_client)
    first, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path
    )
    revisions = base + "/documents/" + str(artifact.document_id) + "/revisions"
    response = upload(
        client, revisions, {**h, "Idempotency-Key": str(uuid4())}, b"Unpublished replacement."
    ).json()
    jobs, outbox, publisher, delivery = services
    second = await jobs.claim("ingest", "pending")
    fixture_vectors(database, object_store, second, response, tmp_path)
    await publisher.stage(scope(second), second)
    await delivery.run(await outbox.claim())
    result = await retriever.retrieve(scope(first), "Fixture evidence", [artifact.kb_id])
    assert {e.passage.revision_id for e in result.evidence} == {first.revision_id}
    with pytest.raises(Problem, match="not_found"):
        await retriever.retrieve_revisions(
            scope(first), "Compare", [first.revision_id, second.revision_id]
        )
    assert await publisher.finalize(scope(second), second)
    result = await retriever.retrieve(scope(first), "Fixture evidence", [artifact.kb_id])
    assert {e.passage.revision_id for e in result.evidence} == {second.revision_id}
    comparison = await retriever.retrieve_revisions(
        scope(first), "Compare", [first.revision_id, second.revision_id]
    )
    assert {e.passage.revision_id for e in comparison.evidence} == {
        first.revision_id,
        second.revision_id,
    }
    assert len(comparison.evidence) <= 20
    assert [r.revision_id for r in comparison.scope.revisions] == [
        first.revision_id,
        second.revision_id,
    ]
    reverse = await retriever.retrieve_revisions(
        scope(first), "Compare", [second.revision_id, first.revision_id]
    )
    assert [r.revision_id for r in reverse.scope.revisions] == [
        second.revision_id,
        first.revision_id,
    ]


@pytest.mark.parametrize("failure", ["dense", "sparse", "both"])
async def test_branch_transport_failures_are_explicit_without_widening_filters(
    auth_client, object_store, database, services, retriever, tmp_path, monkeypatch, failure
):
    client, _, _, h, _, url = setup(auth_client)
    lease, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path
    )

    async def failed_batch(*args, **kwargs):
        raise TimeoutError("scripted batch transport failure")

    original = retriever.index.client.query_points

    async def branch(*args, **kwargs):
        using = kwargs["using"]
        conditions = {c.key: c.match for c in kwargs["query_filter"].must}
        assert conditions["workspace_id"].value == str(lease.workspace_id)
        assert conditions["revision_id"].any == [str(lease.revision_id)]
        if failure == "both" or using == ("bm25" if failure == "sparse" else "dense"):
            raise TimeoutError("scripted single branch failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(retriever.index.client, "query_batch_points", failed_batch)
    monkeypatch.setattr(retriever.index.client, "query_points", branch)
    if failure == "both":
        with pytest.raises(Problem, match="retrieval_unavailable"):
            await retriever.retrieve(scope(lease), "Fixture", [artifact.kb_id])
    else:
        result = await retriever.retrieve(scope(lease), "Fixture", [artifact.kb_id])
        assert result.evidence
        assert failure + "_unavailable" in result.degraded


@pytest.mark.parametrize("change", ["membership", "tombstone", "active_pointer"])
async def test_reauthorization_after_vector_query_fences_revocation_and_recomputes_once(
    auth_client, object_store, database, services, retriever, tmp_path, monkeypatch, change
):
    client, _, _, h, base, url = setup(auth_client)
    lease, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path
    )
    original = retriever.index.query
    called = False

    async def changing(*args, **kwargs):
        nonlocal called
        result = await original(*args, **kwargs)
        if not called:
            called = True
            if change == "membership":
                with psycopg.connect(database["operator"]) as conn:
                    conn.execute(
                        "DELETE FROM app.memberships WHERE workspace_id=%s AND user_id=%s",
                        (lease.workspace_id, lease.actor_id),
                    )
            elif change == "tombstone":
                assert (
                    client.delete(
                        base + "/knowledge-bases/" + str(artifact.kb_id), headers=h
                    ).status_code
                    == 202
                )
            else:
                await publish_fixture(
                    client,
                    base + "/documents/" + str(artifact.document_id) + "/revisions",
                    h,
                    database,
                    object_store,
                    services,
                    tmp_path,
                    data=b"New active edition",
                )
        return result

    monkeypatch.setattr(retriever.index, "query", changing)
    if change != "active_pointer":
        with pytest.raises(Problem, match="not_found"):
            await retriever.retrieve(scope(lease), "Fixture", [artifact.kb_id])
    else:
        result = await retriever.retrieve(scope(lease), "Fixture", [artifact.kb_id])
        assert "scope_recomputed" in result.degraded
        assert all(e.passage.revision_id != lease.revision_id for e in result.evidence)


async def test_empty_corpus_and_missing_named_vector_are_distinct(
    auth_client, object_store, database, services, writer, retriever, tmp_path
):
    client, _, _, h, base, url = setup(auth_client)
    kb = UUID(url.split("/")[-2])
    with psycopg.connect(database["operator"]) as conn:
        actor = conn.execute(
            "SELECT user_id FROM app.memberships WHERE workspace_id=%s", (base.rsplit("/", 1)[1],)
        ).fetchone()[0]
    ctx = AuthContext(actor, UUID(base.rsplit("/", 1)[1]), "owner", 1, uuid4())
    result = await retriever.retrieve(ctx, "Empty corpus", [kb])
    assert result.evidence == () and result.scope.revisions == ()
    lease, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path
    )
    await writer.client.delete_vectors(
        writer.config.collection, points=[str(artifact.points[0].id)], vectors=["bm25"], wait=True
    )
    with pytest.raises(Problem, match="index_unavailable"):
        await retriever.retrieve(scope(lease), "Fixture", [kb])


async def test_untrusted_candidate_metadata_is_filtered_before_fusion(
    auth_client, object_store, database, services, retriever, tmp_path, monkeypatch
):
    client, _, _, h, _, url = setup(auth_client)
    lease, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path
    )
    original = retriever.index.query

    async def tampered(*args, **kwargs):
        result = await original(*args, **kwargs)
        bad = replace(
            result.dense[0], payload={**result.dense[0].payload, "manifest_sha256": "0" * 64}
        )
        return replace(result, dense=(bad,) + result.dense[1:])

    monkeypatch.setattr(retriever.index, "query", tampered)
    result = await retriever.retrieve(scope(lease), "Fixture", [artifact.kb_id])
    passage = next(e for e in result.evidence if e.passage.chunk_id == artifact.points[0].id)
    assert passage.dense_rank is None and passage.sparse_rank == 1
    assert "candidate_filtered" in result.degraded


async def test_large_scope_is_rejected_before_query_without_truncation(
    auth_client, object_store, database, services, retriever, tmp_path, monkeypatch
):
    client, _, _, h, _, url = setup(auth_client)
    lease, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path
    )
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            """CREATE TEMP TABLE many_docs AS SELECT gen_random_uuid() AS d,gen_random_uuid() AS r FROM generate_series(1,4096)"""
        )
        conn.execute(
            """INSERT INTO app.documents(workspace_id,id,kb_id,title,created_by,active_revision_id,latest_revision_id)
            SELECT %s,d,%s,'Bounded scope fixture',%s,r,r FROM many_docs""",
            (lease.workspace_id, artifact.kb_id, lease.actor_id),
        )
        conn.execute(
            """INSERT INTO app.document_revisions(workspace_id,id,document_id,kb_id,revision_no,status,
            object_key,mime_type,byte_size,content_sha256,parser_version,chunk_profile_id,profile_hash,embedding_profile_id,chunk_count,published_at)
            SELECT %s,r,d,%s,1,'published','synthetic-scope-fixture','text/plain',1,%s,'parse_v1','structure_v1',%s,%s,1,now() FROM many_docs""",
            (
                lease.workspace_id,
                artifact.kb_id,
                "a" * 64,
                artifact.profile_hash,
                artifact.profile_id,
            ),
        )
        conn.execute(
            """INSERT INTO app.index_manifests(workspace_id,revision_id,profile_id,collection_name,state,
            expected_points,verified_points,manifest_sha256,artifact_key,verified_at)
            SELECT %s,r,%s,%s,'verified',1,1,%s,'synthetic-scope-fixture',now() FROM many_docs""",
            (lease.workspace_id, artifact.profile_id, retriever.config.collection, "a" * 64),
        )

    async def must_not_query(*args, **kwargs):
        raise AssertionError("A too-large scope must not be truncated or queried")

    monkeypatch.setattr(retriever.index, "query", must_not_query)
    with pytest.raises(Problem, match="scope_too_large"):
        await retriever.retrieve(scope(lease), "Fixture", [artifact.kb_id])


@pytest.mark.parametrize("revoke", ["membership", "tombstone"])
async def test_query_cache_hit_still_rechecks_authority(
    auth_client, object_store, database, services, retriever, tmp_path, monkeypatch, revoke
):
    client, _, _, h, base, url = setup(auth_client)
    lease, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path
    )
    calls = []
    original = retriever.encoder.query_dense

    def counted(query):
        calls.append(query)
        return original(query)

    monkeypatch.setattr(retriever.encoder, "query_dense", counted)
    first = await retriever.retrieve(scope(lease), "  Fixture é  ", [artifact.kb_id])
    again = await retriever.retrieve(scope(lease), "Fixture e\u0301", [artifact.kb_id])
    assert first.evidence == again.evidence and len(calls) == 1
    assert len(retriever.query_cache.entries) == 1
    if revoke == "membership":
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "DELETE FROM app.memberships WHERE workspace_id=%s AND user_id=%s",
                (lease.workspace_id, lease.actor_id),
            )
    else:
        assert (
            client.delete(base + "/knowledge-bases/" + str(artifact.kb_id), headers=h).status_code
            == 202
        )
    with pytest.raises(Problem, match="not_found"):
        await retriever.retrieve(scope(lease), "Fixture é", [artifact.kb_id])
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["merge", "failed_variant", "revoked", "foreign_payload"])
async def test_query_variants_keep_original_question_and_identical_live_scope(
    auth_client, object_store, database, services, retriever, tmp_path, monkeypatch, mode
):
    import hashlib

    from evidencedesk.retrieval.models import CandidateLists

    client, _, _, h, base, url = setup(auth_client)
    lease, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path, n=3
    )
    original = retriever.index.query
    scopes = []

    async def variants(ctx, resolved, dense, sparse, **kwargs):
        scopes.append(resolved)
        found = await original(ctx, resolved, dense, sparse, **kwargs)
        if len(scopes) == 1:
            return CandidateLists(found.dense[:2], found.sparse[:2], (), {})
        assert resolved == scopes[0]
        if mode == "failed_variant":
            raise Problem(503, "retrieval_unavailable", "Scripted variant failure")
        if mode == "revoked":
            assert (
                client.delete(
                    base + "/knowledge-bases/" + str(artifact.kb_id), headers=h
                ).status_code
                == 202
            )
        final = found.dense[2]
        if mode == "foreign_payload":
            final = replace(final, payload={**final.payload, "workspace_id": str(uuid4())})
        return CandidateLists((replace(final, rank=1),), (), (), {})

    monkeypatch.setattr(retriever.index, "query", variants)
    if mode == "revoked":
        with pytest.raises(Problem, match="not_found"):
            await retriever.retrieve_variants(
                scope(lease), "What about it?", "Fixture evidence", [artifact.kb_id]
            )
    else:
        result = await retriever.retrieve_variants(
            scope(lease), "What about it?", "Fixture evidence", [artifact.kb_id]
        )
        assert result.query_sha256 == hashlib.sha256(b"What about it?").hexdigest()
        assert len(result.evidence) == (3 if mode == "merge" else 2)
        if mode == "failed_variant":
            assert "rewrite_retrieval_unavailable" in result.degraded
        if mode == "foreign_payload":
            assert "candidate_filtered" in result.degraded
    assert len(scopes) == 2


async def test_full_two_query_candidate_budget_remains_bounded_per_hydration(
    auth_client, object_store, database, services, retriever, tmp_path
):
    client, _, _, h, _, url = setup(auth_client)
    lease, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path, n=50
    )
    result = await retriever.retrieve_variants(
        scope(lease), "What about it?", "Fixture evidence", [artifact.kb_id]
    )
    assert len(result.evidence) == 20
    assert result.candidate_counts["dense"] == result.candidate_counts["rewrite_dense"] == 40
    assert not result.degraded

import math
from uuid import UUID, uuid4

import pytest
from evidencedesk.db.model_slots import local_generation_slot
from evidencedesk.errors import Problem
from evidencedesk.ingestion.sandbox import ParserSandbox
from evidencedesk.workers.embedding import EmbeddingWorker
from evidencedesk.workers.ingest import ExtractionWorker

from tests.integration.retrieval.test_retrieval import publish_fixture
from tests.integration.retrieval.test_retrieval import retriever as retriever
from tests.integration.revisions.test_publication import scope
from tests.security.uploads.test_uploads import setup, upload

pytestmark = pytest.mark.integration


async def test_real_query_encoders_find_hand_authored_gold_passages(
    auth_client,
    object_store,
    database,
    services,
    writer,
    retriever,
    tmp_path,
    encoder,
    parser_image,
):
    client, _, _, h, _, url = setup(auth_client)
    jobs, outbox, publisher, delivery = services
    corpus = [
        (
            "Travel policy",
            "Employees can claim rail tickets and hotel costs. Travel receipts must be submitted within thirty days.",
            "How long do employees have to submit travel receipts?",
        ),
        (
            "Backup policy",
            "Database backups run daily at midnight. Each encrypted backup is retained for thirty days.",
            "When are database backups made?",
        ),
        (
            "Leave policy",
            "Full time staff receive twenty days of annual leave. Leave requests require manager approval.",
            "How many annual leave days do employees receive?",
        ),
        (
            "Support policy",
            "Urgent customer support incidents must receive a response within two hours.",
            "What is the urgent support response deadline?",
        ),
    ]
    expected = []
    lease = None
    for title, text, query in corpus:
        response = upload(
            client, url, {**h, "Idempotency-Key": str(uuid4())}, text.encode(), name=title + ".txt"
        )
        assert response.status_code == 202, response.text
        lease = await jobs.claim("ingest", "gold-fixture")
        parsed = await ExtractionWorker(
            publisher.db, jobs, object_store, ParserSandbox(parser_image)
        ).run(lease)
        await EmbeddingWorker(publisher.db, jobs, object_store, encoder).run(lease, parsed)
        await publisher.stage(scope(lease), lease)
        await delivery.run(await outbox.claim())
        assert await publisher.finalize(scope(lease), lease)
        expected.append((query, lease.revision_id, text))
    retriever.encoder = encoder
    kb = UUID(url.split("/")[-2])
    for profile in ("dense", "hybrid", "sparse_bm25"):
        for query, revision, text in expected:
            result = await retriever.retrieve(scope(lease), query, [kb], profile)
            assert result.evidence[0].passage.revision_id == revision
            assert result.evidence[0].passage.text == text
    vector = encoder.query_dense("How do I request annual leave?")
    assert len(vector) == 384 and math.isclose(sum(v * v for v in vector), 1, abs_tol=1e-5)
    with pytest.raises(Problem, match="query_too_long"):
        encoder.query_dense("word " * 257)


async def test_dense_cpu_slot_sheds_to_sparse_and_query_limits_are_not_truncated(
    auth_client, object_store, database, services, retriever, tmp_path
):
    client, _, _, h, _, url = setup(auth_client)
    lease, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path
    )
    async with local_generation_slot(retriever.db, slot_id=2):
        result = await retriever.retrieve(scope(lease), "Fixture", [artifact.kb_id])
        assert result.evidence and "dense_encoder_unavailable" in result.degraded
        with pytest.raises(Problem, match="retrieval_unavailable"):
            await retriever.retrieve(scope(lease), "Fixture", [artifact.kb_id], "dense")
    with pytest.raises(Problem, match="query_too_long"):
        await retriever.retrieve(scope(lease), "word " * 257, [artifact.kb_id])
    with pytest.raises(Problem, match="invalid_scope"):
        await retriever.retrieve(scope(lease), "Fixture", [artifact.kb_id] * 2)


async def test_stopword_only_documents_have_valid_empty_sparse_vectors(
    auth_client,
    object_store,
    database,
    services,
    writer,
    retriever,
    tmp_path,
    encoder,
    parser_image,
):
    client, _, _, h, _, url = setup(auth_client)
    response = upload(client, url, h, b"and the or")
    assert response.status_code == 202, response.text
    jobs, outbox, publisher, delivery = services
    lease = await jobs.claim("ingest", "stopword-fixture")
    parsed = await ExtractionWorker(
        publisher.db, jobs, object_store, ParserSandbox(parser_image)
    ).run(lease)
    artifact = await EmbeddingWorker(publisher.db, jobs, object_store, encoder).run(lease, parsed)
    assert not artifact.points[0].sparse.indices
    await publisher.stage(scope(lease), lease)
    await delivery.run(await outbox.claim())
    assert await publisher.finalize(scope(lease), lease)
    retriever.encoder = encoder
    result = await retriever.retrieve(scope(lease), "and the or", [artifact.kb_id])
    assert result.evidence and result.evidence[0].passage.revision_id == lease.revision_id

import socket
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
from evidencedesk.embeddings.encoder import Encoder
from evidencedesk.embeddings.models import SparseVector, VectorArtifact, VectorPoint
from evidencedesk.errors import Problem
from evidencedesk.ingestion.blocks import SourceLocator
from evidencedesk.ingestion.chunking import Chunker
from evidencedesk.ingestion.parsers.core import Builder
from pydantic import ValidationError

pytestmark = pytest.mark.models


@pytest.fixture(scope="module")
def encoder():
    return Encoder(Path(".cache/models"), Path("contracts/config/model-artifacts.lock.json"))


def chunks(encoder):
    builder = Builder("a" * 64, "text/plain")
    for i, text in enumerate(
        ["Evidence supports immutable revisions.", "Bananas grow on plants.", "the and a"]
    ):
        builder.add(text, SourceLocator(kind="text", index=i, page=i + 1))
    builder.pages = 3
    return Chunker(encoder.tokenizer).build(builder.finish(), *[uuid4() for _ in range(4)])


def test_real_cpu_dense_sparse_order_norm_and_offline_artifacts(monkeypatch):
    attempts = []

    def no_network(self, address):
        attempts.append(address)
        raise AssertionError("Embedding must be offline")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    model = Encoder(Path(".cache/models"), Path("contracts/config/model-artifacts.lock.json"))
    source = chunks(model)
    points = model.embed(source)
    assert len(points) == 3
    assert [p.id for p in points] == [c.id for c in source]
    assert all(len(p.dense) == 384 and abs(np.linalg.norm(p.dense) - 1) < 1e-3 for p in points)
    assert points[0].sparse.indices and model.query_sparse("immutable revision").indices
    assert points[2].sparse.indices == []
    repeated = model.embed([source[0]])[0]
    assert np.allclose(repeated.dense, points[0].dense, atol=1e-6)
    assert repeated.sparse == points[0].sparse
    assert not attempts


def test_invalid_token_counts_and_numerical_provider_output(encoder, monkeypatch):
    source = chunks(encoder)
    with pytest.raises(Problem) as exc:
        encoder.embed([replace(source[0], token_count=1)])
    assert exc.value.code == "tokenizer_mismatch"
    for array in [np.zeros((3, 384)), np.full((3, 384), np.nan), np.zeros((3, 383))]:
        monkeypatch.setattr(encoder.dense, "encode", lambda *a, result=array, **kw: result)
        with pytest.raises(Problem) as exc:
            encoder.embed(source)
        assert exc.value.code == "invalid_vectors"


def test_batch_memory_failure_is_safe_and_retryable(encoder, monkeypatch):
    def oom(*args, **kwargs):
        raise MemoryError("Internal source-containing error must not escape")

    monkeypatch.setattr(encoder.dense, "encode", oom)
    with pytest.raises(Problem) as exc:
        encoder.embed(chunks(encoder))
    assert exc.value.code == "embedding_resource_failure" and exc.value.retryable
    assert "source-containing" not in exc.value.detail


def test_vector_envelope_rejects_duplicate_indices_and_wrong_order():
    for indices, values in [
        ([1, 1], [1.0, 2.0]),
        ([2, 1], [1.0, 2.0]),
        ([1], [float("inf")]),
        ([1], []),
        ([1], [0.0]),
    ]:
        with pytest.raises(ValidationError):
            SparseVector(indices=indices, values=values)
    point = VectorPoint(
        id=uuid4(),
        ordinal=1,
        text_sha256="a" * 64,
        dense=[1.0] + [0.0] * 383,
        sparse=SparseVector(indices=[], values=[]),
    )
    with pytest.raises(ValidationError):
        VectorArtifact(
            workspace_id=uuid4(),
            kb_id=uuid4(),
            document_id=uuid4(),
            revision_id=uuid4(),
            profile_id="fixture",
            profile_hash="a" * 64,
            blocks_sha256="b" * 64,
            points=[point],
        )

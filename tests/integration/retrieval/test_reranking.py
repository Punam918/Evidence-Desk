import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from evidencedesk.db.model_slots import local_generation_slot
from evidencedesk.errors import Problem
from evidencedesk.retrieval.rerank import Reranker
from evidencedesk.retrieval.rerank_process import RerankProcess, RerankUnavailable
from evidencedesk.retrieval.rerank_protocol import Pair, Score, ScoreRequest, ScoreResponse

from tests.integration.retrieval.test_retrieval import publish_fixture
from tests.integration.retrieval.test_retrieval import retriever as retriever
from tests.integration.revisions.test_publication import scope
from tests.security.uploads.test_uploads import setup

pytestmark = pytest.mark.integration


class ScriptedScorer:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def score(self, request):
        self.calls.append(request)
        if self.fail:
            raise RerankUnavailable()
        return ScoreResponse(
            fingerprint="a" * 64,
            scores=[
                Score(
                    id=p.id,
                    logit=1.0,
                    query_tokens=1,
                    document_tokens=1,
                    pair_tokens=5,
                    query_clipped=False,
                    document_clipped=False,
                )
                for p in request.passages
            ],
        )


async def test_live_authorization_ties_busy_and_unavailable_fallback(
    auth_client,
    object_store,
    database,
    services,
    retriever,
    tmp_path,
):
    client, _, _, h, _, url = setup(auth_client)
    lease, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path
    )
    result = await retriever.retrieve(scope(lease), "Fixture", [artifact.kb_id], "hybrid_rerank")
    scorer = ScriptedScorer()
    reranker = Reranker(retriever.db, scorer)
    ranked = await reranker.rank(scope(lease), "Fixture", result)
    assert ranked.evidence == result.evidence and len(scorer.calls) == 1
    assert ranked.fingerprint and not ranked.degraded
    async with local_generation_slot(retriever.db, slot_id=3):
        ranked = await reranker.rank(scope(lease), "Fixture", result)
        assert ranked.evidence == result.evidence and "rerank_unavailable" in ranked.degraded
        assert len(scorer.calls) == 1
    scorer.fail = True
    ranked = await reranker.rank(scope(lease), "Fixture", result)
    assert ranked.evidence == result.evidence and "rerank_unavailable" in ranked.degraded
    calls = len(scorer.calls)
    foreign = replace(
        result.evidence[0], passage=replace(result.evidence[0].passage, chunk_id=uuid4())
    )
    with pytest.raises(Problem, match="source_revoked"):
        await reranker.rank(scope(lease), "Fixture", replace(result, evidence=(foreign,)))
    forged = replace(
        result.evidence[0], passage=replace(result.evidence[0].passage, text="not stored source")
    )
    with pytest.raises(Problem, match="source_revoked"):
        await reranker.rank(scope(lease), "Fixture", replace(result, evidence=(forged,)))
    with pytest.raises(Problem, match="invalid_evidence"):
        await reranker.rank(scope(lease), "Fixture", replace(result, evidence=result.evidence * 2))
    assert len(scorer.calls) == calls
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "DELETE FROM app.memberships WHERE workspace_id=%s AND user_id=%s",
            (lease.workspace_id, lease.actor_id),
        )
    with pytest.raises(Problem):
        await reranker.rank(scope(lease), "Fixture", result)
    assert len(scorer.calls) == calls


@pytest.mark.models
async def test_real_offline_minilm_order_clipping_and_batch_bounds(monkeypatch):
    process = RerankProcess(
        Path(".cache/models"), Path("contracts/config/model-artifacts.lock.json")
    )
    original = __import__("asyncio").create_subprocess_exec

    async def spawn(*args, **kwargs):
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        assert not any(k.startswith("ED_") for k in kwargs["env"])
        return await original(*args, **kwargs)

    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    measurements = []
    try:
        await process.start()
        request = ScoreRequest(
            query="How long are backups retained?",
            passages=[
                Pair(id=UUID(int=1), text="Annual leave requires manager approval."),
                Pair(
                    id=UUID(int=2), text="Encrypted database backups are retained for thirty days."
                ),
                Pair(id=UUID(int=3), text="Urgent incidents need a response within two hours."),
            ],
        )
        started = time.perf_counter()
        result = await process.score(request)
        measurements.append(
            {"kind": "three_short_passages", "ms": (time.perf_counter() - started) * 1000}
        )
        assert [s.id for s in result.scores] == [p.id for p in request.passages]
        assert max(result.scores, key=lambda s: s.logit).id == UUID(int=2)
        # Twenty max-length pairs cross all three batch boundaries. Only scoring copies clip.
        long = ScoreRequest(
            query="head " * 55 + "middle " * 15 + "tail " * 55,
            passages=[Pair(id=UUID(int=i + 1), text="backup retention " * 500) for i in range(20)],
        )
        before = long.model_dump_json()
        started = time.perf_counter()
        try:
            clipped = await process.score(long)
        except RerankUnavailable:
            measurements.append(
                {
                    "kind": "twenty_512_token_pairs",
                    "outcome": "hard_deadline_fallback",
                    "ms": (time.perf_counter() - started) * 1000,
                }
            )
            # A capacity miss is honest evidence, not a flaky assertion of hardware speed.
            assert process.process is None
        else:
            measurements.append(
                {
                    "kind": "twenty_512_token_pairs",
                    "outcome": "scored",
                    "ms": (time.perf_counter() - started) * 1000,
                }
            )
            assert len(clipped.scores) == 20
            assert all(
                s.query_clipped and s.document_clipped and s.pair_tokens == 512
                for s in clipped.scores
            )
            assert [s.id for s in clipped.scores] == [p.id for p in long.passages]
            assert (
                max(s.logit for s in clipped.scores) - min(s.logit for s in clipped.scores) < 1e-4
            )
        assert before == long.model_dump_json()
        Path("docs/10-testing/reranker-smoke.json").write_text(
            json.dumps(
                {
                    "kind": "synthetic_cpu_smoke_not_quality_benchmark",
                    "fingerprint": result.fingerprint,
                    "threads": 4,
                    "batch_size": 8,
                    "hard_deadline_seconds": 2,
                    "dataset_sha256": hashlib.sha256(
                        (request.model_dump_json() + before).encode()
                    ).hexdigest(),
                    "measurements": measurements,
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        await process.close()

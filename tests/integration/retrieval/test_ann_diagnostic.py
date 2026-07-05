"""Synthetic filtered ANN diagnostic. This is not a semantic retrieval-quality benchmark."""

import asyncio
import hashlib
import json
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from uuid import UUID, uuid5

import numpy as np
import pytest
from evidencedesk.db.session import AuthContext
from evidencedesk.retrieval.index import SearchIndex
from evidencedesk.retrieval.models import ResolvedScope, RevisionScope
from qdrant_client import models

pytestmark = pytest.mark.integration
NAMESPACE = UUID("aa59b9c8-7809-4f81-9d94-718c70ec52de")


async def test_filtered_ann_against_exact_with_tenant_skew_and_invisible_revisions(writer):
    rng = np.random.default_rng(20261011)
    vectors = rng.normal(size=(5000, 384)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    # Force an indexed diagnostic on this disposable collection only. Serving remains exact=True.
    await writer.client.update_collection(
        writer.config.collection,
        optimizers_config=models.OptimizersConfigDiff(indexing_threshold=1),
        hnsw_config=models.HnswConfigDiff(full_scan_threshold=10, m=16, ef_construct=100),
    )
    portions = [
        ("one_percent", 50),
        ("ten_percent", 500),
        ("eighty_percent", 4000),
        ("remaining", 450),
    ]
    scopes = []
    cursor = 0
    points = []
    dataset = hashlib.sha256(vectors.tobytes())
    for tenant, total in portions:
        workspace, kb, document, actor = [
            uuid5(NAMESPACE, tenant + ":" + label)
            for label in ("workspace", "kb", "document", "actor")
        ]
        revision = uuid5(NAMESPACE, tenant + ":active")
        active_ids = []
        for local in range(total):
            status = (
                "active"
                if local < total * 0.8
                else "staging"
                if local < total * 0.9
                else "superseded"
                if local < total * 0.95
                else "tombstoned"
            )
            uid = uuid5(NAMESPACE, str(cursor))
            payload = {
                "workspace_id": str(workspace),
                "kb_id": str(kb),
                "document_id": str(document),
                "revision_id": str(
                    revision if status == "active" else uuid5(NAMESPACE, tenant + ":" + status)
                ),
                "profile_id": "bge_small_en_v15",
                "text_sha256": hashlib.sha256(str(cursor).encode()).hexdigest(),
                "manifest_sha256": "a" * 64,
            }
            points.append(
                models.PointStruct(
                    id=str(uid),
                    vector={
                        "dense": vectors[cursor].tolist(),
                        "bm25": models.SparseVector(indices=[cursor + 1], values=[1.0]),
                    },
                    payload=payload,
                )
            )
            dataset.update(
                json.dumps({"id": str(uid), "payload": payload}, sort_keys=True).encode()
            )
            if status == "active":
                active_ids.append((uid, cursor))
            cursor += 1
        scopes.append(
            (
                tenant,
                AuthContext(actor, workspace, "viewer", 1, uuid5(NAMESPACE, tenant + ":request")),
                ResolvedScope(
                    workspace,
                    actor,
                    (kb,),
                    (RevisionScope(revision, document, kb, "a" * 64, len(active_ids)),),
                    writer.config.collection,
                    "bge_small_en_v15",
                    1,
                    1,
                    "ask",
                ),
                active_ids,
            )
        )
    for offset in range(0, len(points), 100):
        await writer.client.upsert(
            writer.config.collection,
            points[offset : offset + 100],
            wait=True,
            ordering=models.WriteOrdering.STRONG,
        )
    deadline = time.monotonic() + 60
    while True:
        info = await writer.client.get_collection(writer.config.collection)
        if (info.indexed_vectors_count or 0) >= 10000 and str(info.status) == "green":
            break
        assert time.monotonic() < deadline, "The synthetic ANN diagnostic did not finish indexing"
        await asyncio.sleep(0.5)
    search = SearchIndex(writer.client)
    reports = []
    for tenant, ctx, scope, active in scopes[:3]:
        recalls = []
        allowed = {uid for uid, _ in active}
        for trial in range(20):
            _, index = active[trial * len(active) // 20]
            query = vectors[index] + rng.normal(0, 0.025, 384).astype(np.float32)
            query /= np.linalg.norm(query)
            exact = await search.query(ctx, scope, query.tolist(), None, exact=True)
            ann = await search.query(ctx, scope, query.tolist(), None, exact=False)
            exact_ids = {c.id for c in exact.dense[:20]}
            ann_ids = {c.id for c in ann.dense[:20]}
            assert len(exact_ids) == 20 and ann_ids <= allowed and exact_ids <= allowed
            recalls.append(len(exact_ids & ann_ids) / 20)
        reports.append(
            {
                "tenant": tenant,
                "eligible_points": len(active),
                "queries": 20,
                "ann_recall_at_20": recalls,
                "mean": mean(recalls),
                "minimum": min(recalls),
            }
        )
    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "purpose": "synthetic filtered ANN-vs-exact diagnostic; no semantic quality or production latency claim",
        "seed": 20261011,
        "points": 5000,
        "dimensions": 384,
        "dataset_sha256": dataset.hexdigest(),
        "query_count": 60,
        "query_settings": {
            "branch_limit": 40,
            "recall_cutoff": 20,
            "hnsw_ef": None,
            "default_hnsw_ef": "server default; no explicit per-query tuning",
        },
        "excluded_per_tenant": "20% staging/superseded/tombstoned; all other tenants excluded by filter",
        "serving_dense_exact": True,
        "promotion": "not promoted; this synthetic experiment is insufficient for a production ANN default",
        "qdrant_image": "qdrant/qdrant@sha256:b7b0444c4c351c970b98e90a6f89c2ee4287c65b44e52b4cb503fa5b2aa927ad",
        "indexed_vectors_count": info.indexed_vectors_count,
        "collection_config": info.config.model_dump(mode="json"),
        "hardware": {
            "machine": platform.machine(),
            "processor": platform.processor(),
            "container_cpus": 2,
            "container_memory_bytes": 1073741824,
        },
        "limitations": [
            "Qdrant may choose exact evaluation for some highly selective filters even with an indexed collection.",
            "Scope objects are synthetic for this transport diagnostic; separate real PostgreSQL tests establish authorization.",
            "Single seeded collection; no confidence interval, relevance-label or workload generalization claim.",
        ],
        "results": reports,
    }
    Path("docs/10-testing/retrieval-ann-diagnostic.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )

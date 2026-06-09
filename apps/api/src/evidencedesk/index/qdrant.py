"""Derived index writes and exact manifest verification. Never an authorization authority."""

import hashlib
import math
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import yaml
from evidencedesk.embeddings.models import VectorArtifact, VectorPoint
from evidencedesk.errors import Problem
from evidencedesk.identity.crypto import canonical_json
from evidencedesk.ingestion.profile import IngestionProfile
from qdrant_client import AsyncQdrantClient, models


class IndexConfig:
    def __init__(self, path: Path = Path("contracts/config/qdrant.yaml")):
        self.value = yaml.safe_load(path.read_text())
        profile = IngestionProfile()
        self.create = models.CreateCollection.model_validate(self.value["create_collection"])
        # This generation implements one reviewed layout; a changed layout needs its own migration.
        if (
            not re.fullmatch(r"[a-z0-9_]{1,80}", self.value["collection_prefix"])
            or set(self.value["create_collection"])
            != {
                "vectors",
                "sparse_vectors",
                "on_disk_payload",
                "replication_factor",
                "write_consistency_factor",
                "strict_mode_config",
            }
            or self.value["point_id"] != "chunk_uuid"
            or self.value["sparse_encoder"]
            != {
                "library": "fastembed",
                "model": "Qdrant/bm25",
                "language": profile.sparse_language,
                "k": profile.sparse_k,
                "b": profile.sparse_b,
                "avg_len": profile.sparse_avg_len,
                "defaults_status": "verified_fastembed_0.9.0",
                "token_max_length": profile.sparse_token_max_length,
                "disable_stemmer": False,
            }
            or self.create.vectors
            != {
                "dense": models.VectorParams(
                    size=384, distance=models.Distance.COSINE, on_disk=True
                )
            }
            or self.create.sparse_vectors
            != {
                "bm25": models.SparseVectorParams(
                    modifier=models.Modifier.IDF, index=models.SparseIndexParams(on_disk=True)
                )
            }
            or self.create.replication_factor != 1
            or self.create.write_consistency_factor != 1
            or self.create.on_disk_payload is not True
            or self.create.strict_mode_config
            != models.StrictModeConfig(
                enabled=True,
                unindexed_filtering_retrieve=False,
                unindexed_filtering_update=False,
                max_query_limit=100,
            )
            or self.value["write"] != {"wait": True, "ordering": "strong"}
            or self.value["payload_indexes"]
            != [
                {
                    "field_name": "workspace_id",
                    "field_schema": {"type": "keyword", "is_tenant": True},
                },
                *[
                    {"field_name": key, "field_schema": "keyword"}
                    for key in ("kb_id", "document_id", "revision_id", "profile_id")
                ],
            ]
        ):
            raise ValueError("Index configuration does not match this implemented generation")
        self.hash = hashlib.sha256(
            canonical_json(
                {
                    "qdrant": self.value,
                    "dense_revision": profile.dense_revision,
                    "sparse_revision": profile.sparse_revision,
                    "sparse_library": profile.sparse_library,
                }
            ).encode()
        ).hexdigest()
        self.collection = self.value["collection_prefix"] + "_" + self.hash[:12]


def client(endpoint: str, key: str) -> AsyncQdrantClient:
    url = urlsplit(endpoint)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.query
        or url.fragment
        or url.path not in ("", "/")
        or (
            url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1", "qdrant")
        )
    ):
        raise ValueError(
            "Qdrant requires an operator HTTPS origin or private loopback/Compose origin"
        )
    if not key:
        raise ValueError("Qdrant requires an explicit credential")
    return AsyncQdrantClient(
        url=endpoint, api_key=key, timeout=3, prefer_grpc=False, trust_env=False
    )


class IndexWriter:
    def __init__(self, connection: AsyncQdrantClient, config: IndexConfig):
        self.client, self.config = connection, config

    async def provision(self) -> None:
        name = self.config.collection
        if not await self.client.collection_exists(name):
            await self.client.create_collection(
                name,
                vectors_config=self.config.create.vectors,
                sparse_vectors_config=self.config.create.sparse_vectors,
                on_disk_payload=self.config.create.on_disk_payload,
                replication_factor=self.config.create.replication_factor,
                write_consistency_factor=self.config.create.write_consistency_factor,
                strict_mode_config=self.config.create.strict_mode_config,
            )
        info = await self.client.get_collection(name)
        vectors = info.config.params.vectors
        sparse = info.config.params.sparse_vectors
        strict = info.config.strict_mode_config
        if (
            not isinstance(vectors, dict)
            or set(vectors) != {"dense"}
            or vectors["dense"].size != 384
            or vectors["dense"].distance != models.Distance.COSINE
            or vectors["dense"].on_disk is not True
            or not info.config.params.on_disk_payload
            or info.config.params.replication_factor != 1
            or info.config.params.write_consistency_factor != 1
            or strict is None
            or any(getattr(strict, k) != v for k, v in self.value_strict().items())
            or not sparse
            or set(sparse) != {"bm25"}
            or sparse["bm25"].modifier != models.Modifier.IDF
            or sparse["bm25"].index is None
            or sparse["bm25"].index.on_disk is not True
        ):
            raise Problem(
                409,
                "index_config_mismatch",
                "Existing collection does not match the frozen index profile.",
            )

        for field in ("workspace_id", "kb_id", "document_id", "revision_id", "profile_id"):
            await self.client.create_payload_index(
                name,
                field,
                field_schema=models.KeywordIndexParams(
                    type=models.KeywordIndexType.KEYWORD, is_tenant=field == "workspace_id"
                ),
                wait=True,
                ordering=models.WriteOrdering.STRONG,
            )

    def value_strict(self) -> dict[str, Any]:
        value: dict[str, Any] = self.config.value["create_collection"]["strict_mode_config"]
        return value

    @staticmethod
    def payload(artifact: VectorArtifact, point: VectorPoint, sha: str) -> dict[str, str]:
        return {
            "workspace_id": str(artifact.workspace_id),
            "kb_id": str(artifact.kb_id),
            "document_id": str(artifact.document_id),
            "revision_id": str(artifact.revision_id),
            "profile_id": artifact.profile_id,
            "text_sha256": point.text_sha256,
            "manifest_sha256": sha,
        }

    @staticmethod
    def scope(artifact: VectorArtifact) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(key=field, match=models.MatchValue(value=value))
                for field, value in [
                    ("workspace_id", str(artifact.workspace_id)),
                    ("revision_id", str(artifact.revision_id)),
                ]
            ]
        )

    async def upsert(
        self, artifact: VectorArtifact, sha: str, guard: Callable[[], Awaitable[None]]
    ) -> None:
        for offset in range(0, len(artifact.points), 100):
            await guard()
            points = [
                models.PointStruct(
                    id=str(p.id),
                    vector={
                        "dense": p.dense,
                        "bm25": models.SparseVector(
                            indices=p.sparse.indices, values=p.sparse.values
                        ),
                    },
                    payload=self.payload(artifact, p, sha),
                )
                for p in artifact.points[offset : offset + 100]
            ]
            await self.client.upsert(
                self.config.collection, points, wait=True, ordering=models.WriteOrdering.STRONG
            )

    async def verify(
        self, artifact: VectorArtifact, sha: str, guard: Callable[[], Awaitable[None]]
    ) -> None:
        expected = {str(p.id): p for p in artifact.points}
        await guard()
        count = await self.client.count(
            self.config.collection, count_filter=self.scope(artifact), exact=True
        )
        if count.count != len(expected):
            raise Problem(
                422,
                "index_count_mismatch",
                "The derived index has an incomplete or extra point set.",
            )
        seen: set[str] = set()
        offset: Any = None
        while True:
            await guard()
            rows, offset = await self.client.scroll(
                self.config.collection,
                scroll_filter=self.scope(artifact),
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            for row in rows:
                key = str(row.id)
                if (
                    key not in expected
                    or key in seen
                    or row.payload != self.payload(artifact, expected[key], sha)
                ):
                    raise Problem(
                        422,
                        "index_hash_mismatch",
                        "The derived index identities or hashes do not match.",
                    )
                point = expected[key]
                vector = row.vector
                if not isinstance(vector, dict) or set(vector) != {"dense", "bm25"}:
                    raise Problem(
                        422, "index_vector_mismatch", "A required index vector is missing."
                    )
                dense, sparse = vector["dense"], vector["bm25"]
                if (
                    not isinstance(dense, list)
                    or len(dense) != 384
                    or not isinstance(sparse, models.SparseVector)
                    or sparse.indices != point.sparse.indices
                    or len(sparse.values) != len(point.sparse.values)
                    or any(
                        not isinstance(a, (int, float))
                        or not math.isclose(a, b, abs_tol=1e-5, rel_tol=1e-5)
                        for a, b in zip(dense, point.dense, strict=True)
                    )
                    or any(
                        not math.isclose(a, b, abs_tol=1e-5, rel_tol=1e-5)
                        for a, b in zip(sparse.values, point.sparse.values, strict=True)
                    )
                ):
                    raise Problem(
                        422,
                        "index_vector_mismatch",
                        "Index vector content does not match its immutable artifact.",
                    )
                seen.add(key)
            if offset is None:
                break
            if len(seen) >= len(expected):
                raise Problem(
                    422, "index_count_mismatch", "The derived index changed during verification."
                )
        if seen != set(expected):
            raise Problem(
                422,
                "index_count_mismatch",
                "The derived index is missing expected point identities.",
            )

    async def purge(
        self, workspace: UUID, revision: UUID, guard: Callable[[], Awaitable[None]]
    ) -> None:
        await guard()
        if not await self.client.collection_exists(self.config.collection):
            return
        scope = models.Filter(
            must=[
                models.FieldCondition(key=k, match=models.MatchValue(value=str(v)))
                for k, v in [("workspace_id", workspace), ("revision_id", revision)]
            ]
        )
        await guard()
        await self.client.delete(
            self.config.collection,
            models.FilterSelector(filter=scope),
            wait=True,
            ordering=models.WriteOrdering.STRONG,
        )
        await guard()
        count = await self.client.count(self.config.collection, count_filter=scope, exact=True)
        if count.count:
            raise Problem(503, "purge_incomplete", "Derived points remain after deletion.", True)

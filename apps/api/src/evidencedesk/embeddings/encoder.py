"""Pinned CPU dense and sparse encoders; no downloads and no source-text cache."""

from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path

import numpy as np
from evidencedesk.embeddings.artifacts import verified_snapshot
from evidencedesk.embeddings.models import SparseVector, VectorPoint
from evidencedesk.errors import Problem
from evidencedesk.ingestion.chunking import Chunk
from evidencedesk.ingestion.profile import IngestionProfile
from fastembed import SparseTextEmbedding
from pydantic import ValidationError
from sentence_transformers import SentenceTransformer
from tokenizers import Tokenizer


class Encoder:
    QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

    def __init__(self, cache: Path, lock: Path):
        import torch

        dense_path, self.dense_record = verified_snapshot(cache, lock, "embedding")
        sparse_path, self.sparse_record = verified_snapshot(cache, lock, "sparse")
        profile = IngestionProfile()
        if (
            self.dense_record["revision"] != profile.dense_revision
            or self.sparse_record["revision"] != profile.sparse_revision
            or "fastembed==" + version("fastembed") != profile.sparse_library
            or self.sparse_record["settings"]
            != {
                "language": profile.sparse_language,
                "k": profile.sparse_k,
                "b": profile.sparse_b,
                "avg_len": profile.sparse_avg_len,
                "token_max_length": profile.sparse_token_max_length,
                "disable_stemmer": False,
            }
        ):
            raise ValueError("Encoder artifacts/settings do not match the frozen profile")
        torch.set_num_threads(4)
        self.dense = SentenceTransformer(
            str(dense_path),
            device="cpu",
            local_files_only=True,
            trust_remote_code=False,
            model_kwargs={"use_safetensors": True, "local_files_only": True},
        )
        maximum = self.dense.max_seq_length
        if self.dense.get_embedding_dimension() != 384 or maximum is None or maximum < 384:
            raise ValueError("Dense model dimensions/context do not match the frozen profile")
        self.tokenizer = Tokenizer.from_file(str(dense_path / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.sparse = SparseTextEmbedding(
            model_name="Qdrant/bm25",
            specific_model_path=str(sparse_path),
            local_files_only=True,
            cuda=False,
            threads=1,
            **self.sparse_record["settings"],
        )

    def embed(self, chunks: Sequence[Chunk]) -> list[VectorPoint]:
        try:
            return self._embed(chunks)
        except (MemoryError, RuntimeError) as exc:
            raise Problem(
                503, "embedding_resource_failure", "CPU embedding is temporarily unavailable.", True
            ) from exc
        except ValidationError as exc:
            raise Problem(
                422, "invalid_vectors", "Embedding output failed numerical validation."
            ) from exc

    def _embed(self, chunks: Sequence[Chunk]) -> list[VectorPoint]:
        if not 1 <= len(chunks) <= 5000 or len({c.id for c in chunks}) != len(chunks):
            raise ValueError("Invalid embedding batch identities")
        points: list[VectorPoint] = []
        for offset in range(0, len(chunks), 32):
            batch = chunks[offset : offset + 32]
            texts = [c.embedding_text for c in batch]
            for chunk in batch:
                count = len(
                    self.tokenizer.encode(chunk.embedding_text, add_special_tokens=True).ids
                )
                if count != chunk.token_count or count > 384:
                    raise Problem(
                        422,
                        "tokenizer_mismatch",
                        "Embedding input does not match the frozen token count.",
                    )
            dense = self.dense.encode(
                texts,
                batch_size=32,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            if dense.shape != (len(batch), 384) or not np.isfinite(dense).all():
                raise Problem(
                    422, "invalid_vectors", "Embedding dimensions or numerical values are invalid."
                )
            sparse = list(self.sparse.embed(texts, batch_size=32, parallel=None))
            if len(sparse) != len(batch):
                raise Problem(422, "invalid_vectors", "Sparse embedding count is inconsistent.")
            for index, (chunk, lexical) in enumerate(zip(batch, sparse, strict=True)):
                pairs = sorted(zip(lexical.indices.tolist(), lexical.values.tolist(), strict=True))
                points.append(
                    VectorPoint(
                        id=chunk.id,
                        ordinal=chunk.ordinal,
                        text_sha256=chunk.text_sha256,
                        dense=dense[index].tolist(),
                        sparse=SparseVector(
                            indices=[int(i) for i, _ in pairs], values=[float(v) for _, v in pairs]
                        ),
                    )
                )
        if [(p.id, p.text_sha256) for p in points] != [(c.id, c.text_sha256) for c in chunks]:
            raise Problem(422, "vector_order_mismatch", "Embedding output order is inconsistent.")
        return points

    def query_sparse(self, text: str) -> SparseVector:
        result = list(self.sparse.query_embed(text))
        if len(result) != 1:
            raise Problem(503, "invalid_vectors", "Sparse query encoding is unavailable.", True)
        pairs = sorted(zip(result[0].indices.tolist(), result[0].values.tolist(), strict=True))
        return SparseVector(indices=[int(i) for i, _ in pairs], values=[float(v) for _, v in pairs])

    def query_dense(self, text: str) -> list[float]:
        if len(self.tokenizer.encode(text, add_special_tokens=True).ids) > 256:
            raise Problem(422, "query_too_long", "Query exceeds the 256 embedding-token limit.")
        value = self.QUERY_INSTRUCTION + text
        if len(self.tokenizer.encode(value, add_special_tokens=True).ids) > 384:
            raise Problem(
                422, "query_too_long", "Query plus retrieval instruction exceeds the model limit."
            )
        try:
            encoded = self.dense.encode(
                [value], normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
            )
        except (MemoryError, RuntimeError) as exc:
            raise Problem(
                503,
                "embedding_resource_failure",
                "Dense query encoding is temporarily unavailable.",
                True,
            ) from exc
        if (
            encoded.shape != (1, 384)
            or not np.isfinite(encoded).all()
            or abs(float(np.linalg.norm(encoded[0])) - 1) > 1e-3
        ):
            raise Problem(
                503, "invalid_vectors", "Dense query encoding failed numerical validation.", True
            )
        return [float(v) for v in encoded[0]]

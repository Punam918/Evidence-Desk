"""Fenced chunk/embedding stage; queue and CPU concurrency are bounded in PostgreSQL."""

import asyncio
import hashlib
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from evidencedesk.db import ingestion_repository as repository
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.model_slots import local_generation_slot
from evidencedesk.db.session import AuthContext, authorized_transaction, tenant_transaction
from evidencedesk.db.vector_repository import store_chunks
from evidencedesk.embeddings.encoder import Encoder
from evidencedesk.embeddings.models import VectorArtifact, VectorPoint
from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.ingestion.chunking import Chunker
from evidencedesk.ingestion.profile import IngestionProfile
from evidencedesk.jobs.lease import heartbeat
from evidencedesk.jobs.models import Lease
from evidencedesk.observability.stages import instrument
from evidencedesk.storage.s3 import S3Store
from evidencedesk.workers.ingest import ParsedArtifact
from sqlalchemy.ext.asyncio import AsyncEngine


async def cpu_call[T](operation: Callable[..., T], *args: Any) -> T:
    # A cancelled coroutine cannot stop a Python worker thread. Retain its CPU slot until it exits.
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        with suppress(Exception):
            task.result()
        raise cancelled


class EmbeddingWorker:
    def __init__(self, db: AsyncEngine, jobs: JobRepository, store: S3Store, encoder: Encoder):
        self.db, self.jobs, self.store, self.encoder = db, jobs, store, encoder
        self.profile = IngestionProfile()
        self.chunker = Chunker(encoder.tokenizer, self.profile)

    async def run(self, lease: Lease, parsed: ParsedArtifact) -> VectorArtifact:
        if lease.kind != "ingest" or lease.revision_id is None:
            raise ValueError("Embedding requires an ingestion lease")
        ctx = AuthContext(lease.actor_id, lease.workspace_id, "editor", 0, uuid4())
        try:
            async with heartbeat(self.jobs, lease):
                async with local_generation_slot(self.db, slot_id=2):
                    return await self.embed(ctx, lease, parsed)
        except LeaseLost:
            raise
        except Problem as exc:
            async with tenant_transaction(self.db, ctx) as conn:
                if exc.code == "local_model_busy":
                    await JobRepository.defer_gate(conn, lease)
                else:
                    await repository.fail(conn, lease, exc)
            raise

    @instrument("ingest.embed")
    async def embed(self, ctx: AuthContext, lease: Lease, parsed: ParsedArtifact) -> VectorArtifact:
        assert lease.revision_id is not None
        async with authorized_transaction(self.db, ctx, "editor") as conn:
            row = await repository.state(conn, ctx, lease)
        if (
            row["profile_hash"] != self.profile.fingerprint
            or parsed.manifest.original_sha256 != row["content_sha256"]
            or parsed.sha256 != row["checkpoint"].get("blocks_sha256")
            or parsed.key != row["checkpoint"].get("blocks_key")
        ):
            raise Problem(
                422, "profile_mismatch", "The source or frozen ingestion profile has changed."
            )
        actual = await cpu_call(
            lambda: hashlib.sha256(parsed.manifest.model_dump_json().encode()).hexdigest()
        )
        if actual != parsed.sha256:
            raise Problem(
                422, "invalid_manifest", "The normalized blocks do not match their checkpoint."
            )
        chunks = await cpu_call(
            self.chunker.build,
            parsed.manifest,
            ctx.workspace_id,
            row["document_id"],
            lease.revision_id,
            row["kb_id"],
        )
        async with authorized_transaction(self.db, ctx, "editor") as conn:
            await store_chunks(conn, ctx, lease, chunks)
        prefix = f"workspaces/{ctx.workspace_id}/documents/{row['document_id']}/revisions/{lease.revision_id}/manifest/"
        with TemporaryDirectory(prefix="ed-vectors-") as directory:
            output = Path(directory) / "vectors.json"
            checkpoint = row["checkpoint"]
            if checkpoint.get("vectors_key"):
                key, sha = checkpoint["vectors_key"], checkpoint.get("vectors_sha256", "")
                if not isinstance(sha, str) or len(sha) != 64 or key != prefix + sha:
                    raise Problem(422, "invalid_checkpoint", "The vector checkpoint is invalid.")
                await asyncio.to_thread(self.store.download, key, output, sha, 128 * 1024 * 1024)
                try:
                    artifact = await cpu_call(
                        lambda: VectorArtifact.model_validate_json(output.read_bytes())
                    )
                except ValueError as exc:
                    raise Problem(
                        422, "invalid_vectors", "The stored vector manifest is invalid."
                    ) from exc
            else:
                points: list[VectorPoint] = []
                for offset in range(0, len(chunks), 32):
                    async with authorized_transaction(self.db, ctx, "editor") as conn:
                        await repository.stage(
                            conn,
                            ctx,
                            lease,
                            "embedding",
                            checkpoint,
                            30 + int(40 * offset / len(chunks)),
                        )
                    points.extend(await cpu_call(self.encoder.embed, chunks[offset : offset + 32]))
                    async with authorized_transaction(self.db, ctx, "editor") as conn:
                        await repository.state(conn, ctx, lease)
                artifact = VectorArtifact(
                    workspace_id=ctx.workspace_id,
                    kb_id=row["kb_id"],
                    document_id=row["document_id"],
                    revision_id=lease.revision_id,
                    profile_id=self.profile.embedding_profile_id,
                    profile_hash=self.profile.fingerprint,
                    blocks_sha256=parsed.sha256,
                    points=points,
                )
                content = await cpu_call(artifact.canonical_bytes)
                if len(content) > 128 * 1024 * 1024:
                    raise Problem(422, "manifest_limit", "Vector artifact exceeded its size limit.")
                sha = hashlib.sha256(content).hexdigest()
                output.write_bytes(content)
                key = prefix + sha
                await asyncio.to_thread(self.store.put, key, output, "application/json", sha)
            if (
                artifact.workspace_id,
                artifact.kb_id,
                artifact.document_id,
                artifact.revision_id,
                artifact.profile_id,
                artifact.profile_hash,
                artifact.blocks_sha256,
            ) != (
                ctx.workspace_id,
                row["kb_id"],
                row["document_id"],
                lease.revision_id,
                self.profile.embedding_profile_id,
                self.profile.fingerprint,
                parsed.sha256,
            ) or [(p.id, p.ordinal, p.text_sha256) for p in artifact.points] != [
                (c.id, c.ordinal, c.text_sha256) for c in chunks
            ]:
                raise Problem(
                    422,
                    "vector_identity_mismatch",
                    "Vector identities do not match the authorized chunks.",
                )
            checkpoint = {
                **checkpoint,
                "vectors_key": key,
                "vectors_sha256": sha,
                "chunk_count": len(chunks),
            }
            async with authorized_transaction(self.db, ctx, "editor") as conn:
                await repository.stage(conn, ctx, lease, "verifying", checkpoint, 75)
            return artifact

"""Publication is one PostgreSQL commit after separately acknowledged index verification."""

from uuid import uuid4

from evidencedesk.db import index_repository as index
from evidencedesk.db import ingestion_repository as ingestion
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext, authorized_transaction, tenant_transaction
from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.index.artifacts import load_artifact
from evidencedesk.index.qdrant import IndexConfig
from evidencedesk.ingestion.profile import IngestionProfile
from evidencedesk.jobs.lease import heartbeat
from evidencedesk.jobs.models import Lease
from evidencedesk.observability.stages import instrument
from evidencedesk.storage.s3 import S3Store
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class Publisher:
    def __init__(self, db: AsyncEngine, jobs: JobRepository, store: S3Store, config: IndexConfig):
        self.db, self.jobs, self.store, self.config = db, jobs, store, config
        self.profile = IngestionProfile()

    async def run(self, lease: Lease) -> bool:
        if lease.kind != "ingest" or lease.revision_id is None:
            raise ValueError("Publication requires an ingestion lease")
        ctx = AuthContext(lease.actor_id, lease.workspace_id, "editor", 0, uuid4())
        try:
            async with heartbeat(self.jobs, lease):
                await self.stage(ctx, lease)
                return await self.finalize(ctx, lease)
        except LeaseLost:
            raise
        except Problem as exc:
            async with tenant_transaction(self.db, ctx) as conn:
                await ingestion.fail(conn, lease, exc)
            raise

    async def stage(self, ctx: AuthContext, lease: Lease) -> None:
        async with authorized_transaction(self.db, ctx, "editor") as conn:
            row = await ingestion.state(conn, ctx, lease)
        checkpoint = row["checkpoint"]
        required = ("vectors_key", "vectors_sha256", "chunk_count", "blocks_sha256")
        if (
            any(k not in checkpoint for k in required)
            or row["profile_hash"] != self.profile.fingerprint
        ):
            raise Problem(
                422, "invalid_checkpoint", "A complete frozen vector checkpoint is required."
            )
        snapshot = {
            **row,
            "revision_id": lease.revision_id,
            "artifact_key": checkpoint["vectors_key"],
            "manifest_sha256": checkpoint["vectors_sha256"],
            "profile_id": row["embedding_profile_id"],
            "expected_points": row["chunk_count"],
        }
        artifact = await load_artifact(self.store, snapshot)
        if (
            artifact.blocks_sha256 != checkpoint["blocks_sha256"]
            or len(artifact.points) != checkpoint["chunk_count"]
        ):
            raise Problem(422, "invalid_checkpoint", "Vector and extraction checkpoints disagree.")
        async with authorized_transaction(self.db, ctx, "editor", write=True) as conn:
            current = await ingestion.state(conn, ctx, lease)
            if current["checkpoint"] != checkpoint:
                raise Problem(409, "checkpoint_changed", "The ingestion checkpoint changed.")
            document = (
                (
                    await conn.execute(
                        text("""SELECT version,latest_revision_id FROM app.documents
                WHERE workspace_id=:w AND id=:d FOR UPDATE"""),
                        {"w": ctx.workspace_id, "d": row["document_id"]},
                    )
                )
                .mappings()
                .one()
            )
            if document["latest_revision_id"] != lease.revision_id or document[
                "version"
            ] != checkpoint.get("document_version", document["version"]):
                raise Problem(
                    409, "revision_changed", "A newer document revision has been requested."
                )
            active_profile = (
                await conn.execute(
                    text("SELECT embedding_profile FROM app.workspaces WHERE id=:w"),
                    {"w": ctx.workspace_id},
                )
            ).scalar_one()
            if active_profile != artifact.profile_id:
                raise Problem(409, "profile_mismatch", "The active embedding profile changed.")
            await index.stage(
                conn, artifact, checkpoint["vectors_key"], checkpoint["vectors_sha256"], self.config
            )
            await conn.execute(
                text(
                    "UPDATE app.document_revisions SET manifest_key=:key WHERE workspace_id=:w AND id=:r"
                ),
                {"w": ctx.workspace_id, "r": lease.revision_id, "key": checkpoint["vectors_key"]},
            )
            await ingestion.stage(
                conn,
                ctx,
                lease,
                "verifying",
                {
                    **checkpoint,
                    "document_version": document["version"],
                    "index_config_sha256": self.config.hash,
                    "chunks_sha256": artifact.identity_sha256,
                },
                85,
            )

    @instrument("ingest.publish")
    async def finalize(self, ctx: AuthContext, lease: Lease) -> bool:
        assert lease.revision_id is not None
        async with authorized_transaction(self.db, ctx, "editor", write=True) as conn:
            row = await ingestion.state(conn, ctx, lease)
            document = (
                (
                    await conn.execute(
                        text("""SELECT version,latest_revision_id,active_revision_id
                FROM app.documents WHERE workspace_id=:w AND id=:d FOR UPDATE"""),
                        {"w": ctx.workspace_id, "d": row["document_id"]},
                    )
                )
                .mappings()
                .one()
            )
            checkpoint = row["checkpoint"]
            manifest = await index.manifest(
                conn, ctx.workspace_id, lease.revision_id, self.profile.embedding_profile_id
            )
            if (
                document["latest_revision_id"] != lease.revision_id
                or document["version"] != checkpoint.get("document_version")
                or manifest["tombstoned"]
                or manifest["embedding_profile"] != self.profile.embedding_profile_id
                or row["profile_hash"] != self.profile.fingerprint
                or checkpoint.get("index_config_sha256") != self.config.hash
                or manifest["collection_name"] != self.config.collection
                or manifest["manifest_sha256"] != checkpoint.get("vectors_sha256")
                or manifest["artifact_key"] != checkpoint.get("vectors_key")
                or manifest["expected_points"] != row["chunk_count"]
            ):
                raise Problem(
                    409,
                    "publication_changed",
                    "Source, document version or index configuration changed before publication.",
                )
            if manifest["state"] != "verified":
                dead = (
                    await conn.execute(
                        text("""SELECT 1 FROM app.index_outbox WHERE workspace_id=:w AND revision_id=:r
                    AND profile_id=:p AND operation='upsert' AND state='dead'"""),
                        {
                            "w": ctx.workspace_id,
                            "r": lease.revision_id,
                            "p": self.profile.embedding_profile_id,
                        },
                    )
                ).first()
                if dead:
                    raise Problem(
                        503,
                        "index_delivery_failed",
                        "Index delivery failed. The ingestion job can be retried explicitly.",
                    )
                await JobRepository.defer_gate(conn, lease)
                return False
            chunks = (
                (
                    await conn.execute(
                        text(
                            """SELECT count(*) AS n,bool_and(text_sha256=encode(sha256(convert_to(text,'UTF8')),'hex')) AS valid,
                        encode(sha256(convert_to(string_agg(id::text||':'||ordinal::text||':'||text_sha256,chr(10) ORDER BY ordinal),'UTF8')),'hex') AS sha
                        FROM app.chunks WHERE workspace_id=:w AND revision_id=:r"""
                        ),
                        {"w": ctx.workspace_id, "r": lease.revision_id},
                    )
                )
                .mappings()
                .one()
            )
            if (
                chunks["n"] != manifest["verified_points"]
                or chunks["n"] == 0
                or not chunks["valid"]
                or chunks["sha"] != checkpoint.get("chunks_sha256")
            ):
                raise Problem(
                    422,
                    "chunk_integrity_failure",
                    "Authoritative chunks changed before publication.",
                )
            args = {
                "w": ctx.workspace_id,
                "r": lease.revision_id,
                "d": row["document_id"],
                "old": document["active_revision_id"],
            }
            await conn.execute(
                text(
                    "UPDATE app.document_revisions SET status='superseded' WHERE workspace_id=:w AND id=:old AND status='published'"
                ),
                args,
            )
            await conn.execute(
                text(
                    "UPDATE app.document_revisions SET status='published',published_at=clock_timestamp() WHERE workspace_id=:w AND id=:r"
                ),
                args,
            )
            await conn.execute(
                text(
                    "UPDATE app.documents SET active_revision_id=:r,version=version+1 WHERE workspace_id=:w AND id=:d"
                ),
                args,
            )
            await conn.execute(
                text("UPDATE app.workspaces SET corpus_generation=corpus_generation+1 WHERE id=:w"),
                args,
            )
            await JobRepository.checkpoint(conn, lease, {**checkpoint, "stage": "published"}, 100)
            await JobRepository.finish(conn, lease)
            return True

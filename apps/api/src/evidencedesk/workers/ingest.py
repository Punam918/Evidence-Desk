"""Durable extraction stage. The remaining pipeline consumes its verified block reference."""

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from evidencedesk.db import ingestion_repository as repository
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext, authorized_transaction, tenant_transaction
from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.ingestion.blocks import BlocksManifest
from evidencedesk.ingestion.profile import IngestionProfile
from evidencedesk.ingestion.sandbox import Parser
from evidencedesk.ingestion.validation import MIMES
from evidencedesk.jobs.lease import heartbeat
from evidencedesk.jobs.models import Lease
from evidencedesk.observability.stages import instrument
from evidencedesk.storage.s3 import S3Store
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class ParsedArtifact:
    key: str
    sha256: str
    manifest: BlocksManifest


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class ExtractionWorker:
    def __init__(self, db: AsyncEngine, jobs: JobRepository, store: S3Store, parser: Parser):
        self.db, self.jobs, self.store, self.parser = db, jobs, store, parser

    async def run(self, lease: Lease) -> ParsedArtifact:
        if lease.kind != "ingest" or lease.revision_id is None:
            raise ValueError("Extraction requires an ingestion lease")
        ctx = AuthContext(lease.actor_id, lease.workspace_id, "editor", 0, uuid4())
        try:
            async with heartbeat(self.jobs, lease):
                return await self.extract(ctx, lease)
        except LeaseLost:
            raise
        except Problem as exc:
            async with tenant_transaction(self.db, ctx) as conn:
                await repository.fail(conn, lease, exc)
            raise

    @instrument("ingest.parse")
    async def extract(self, ctx: AuthContext, lease: Lease) -> ParsedArtifact:
        async with authorized_transaction(self.db, ctx, "editor") as conn:
            row = await repository.state(conn, ctx, lease)
            if row["profile_hash"] != IngestionProfile().fingerprint:
                raise Problem(
                    409, "profile_unavailable", "The frozen ingestion profile is not available."
                )
            checkpoint = row["checkpoint"]
        prefix = f"workspaces/{ctx.workspace_id}/documents/{row['document_id']}/revisions/{lease.revision_id}/blocks/"
        with TemporaryDirectory(prefix="ed-ingest-") as directory:
            scratch = Path(directory)
            output = scratch / "blocks.json"
            if checkpoint.get("blocks_key"):
                key, sha = checkpoint["blocks_key"], checkpoint.get("blocks_sha256", "")
                if (
                    not isinstance(key, str)
                    or not isinstance(sha, str)
                    or len(sha) != 64
                    or key != prefix + sha
                ):
                    raise Problem(
                        422, "invalid_checkpoint", "The stored extraction checkpoint is invalid."
                    )
                await asyncio.to_thread(self.store.download, key, output, sha, 256 * 1024 * 1024)
                try:
                    result = await asyncio.to_thread(
                        lambda: BlocksManifest.model_validate_json(output.read_bytes())
                    )
                except ValueError as exc:
                    raise Problem(
                        422, "invalid_manifest", "The stored extraction manifest is invalid."
                    ) from exc
                if (
                    result.original_sha256 != row["content_sha256"]
                    or result.mime_type != row["mime_type"]
                ):
                    raise Problem(
                        422, "invalid_manifest", "The stored extraction identity is invalid."
                    )
                stage = checkpoint.get("stage", "chunking")
                if stage not in ("chunking", "embedding", "verifying"):
                    raise Problem(
                        422, "invalid_checkpoint", "The stored ingestion stage is invalid."
                    )
                async with authorized_transaction(self.db, ctx, "editor") as conn:
                    await repository.stage(conn, ctx, lease, stage, checkpoint, row["progress"])
                return ParsedArtifact(key, sha, result)
            async with authorized_transaction(self.db, ctx, "editor") as conn:
                await repository.stage(conn, ctx, lease, "validating", checkpoint, 5)
            source = scratch / "source"
            await asyncio.to_thread(
                self.store.download, row["object_key"], source, row["content_sha256"]
            )
            source.chmod(0o444)  # Sole file bind mount is readable by the unprivileged parser UID.
            async with authorized_transaction(self.db, ctx, "editor") as conn:
                await repository.stage(conn, ctx, lease, "parsing", checkpoint, 10)
            suffix = next((ext for ext, mime in MIMES.items() if mime == row["mime_type"]), None)
            if suffix is None:
                raise Problem(
                    422, "unsupported_file_type", "The original media type is unsupported."
                )
            result = await self.parser.parse(
                source, "source" + suffix, row["mime_type"], row["content_sha256"], output
            )
            async with authorized_transaction(self.db, ctx, "editor") as conn:
                await repository.stage(conn, ctx, lease, "normalizing", checkpoint, 20)
            sha = await asyncio.to_thread(file_hash, output)
            key = prefix + sha
            await asyncio.to_thread(self.store.put, key, output, "application/json", sha)
            checkpoint = {
                **checkpoint,
                "blocks_key": key,
                "blocks_sha256": sha,
                "original_sha256": row["content_sha256"],
                "profile_hash": row["profile_hash"],
                "parser_image": self.parser.image,
                "block_count": len(result.blocks),
            }
            async with authorized_transaction(self.db, ctx, "editor") as conn:
                await repository.stage(conn, ctx, lease, "chunking", checkpoint, 30)
            return ParsedArtifact(key, sha, result)

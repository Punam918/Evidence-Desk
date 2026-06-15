"""Resume the ingestion stage recorded in PostgreSQL; completed vector work is not repeated."""

from uuid import uuid4

from evidencedesk.db import ingestion_repository as repository
from evidencedesk.db.session import AuthContext, authorized_transaction, tenant_transaction
from evidencedesk.errors import Problem
from evidencedesk.ingestion.publish import Publisher
from evidencedesk.jobs.models import Lease
from evidencedesk.workers.embedding import EmbeddingWorker
from evidencedesk.workers.ingest import ExtractionWorker


class IngestionPipeline:
    def __init__(
        self, extractor: ExtractionWorker, embedder: EmbeddingWorker, publisher: Publisher
    ):
        self.extractor, self.embedder, self.publisher = extractor, embedder, publisher

    async def run(self, lease: Lease) -> bool:
        ctx = AuthContext(lease.actor_id, lease.workspace_id, "editor", 0, uuid4())
        try:
            async with authorized_transaction(self.publisher.db, ctx, "editor") as conn:
                row = await repository.state(conn, ctx, lease)
        except Problem as exc:
            async with tenant_transaction(self.publisher.db, ctx) as conn:
                await repository.fail(conn, lease, exc)
            raise
        if not row["checkpoint"].get("vectors_key"):
            parsed = await self.extractor.run(lease)
            await self.embedder.run(lease, parsed)
        return await self.publisher.run(lease)

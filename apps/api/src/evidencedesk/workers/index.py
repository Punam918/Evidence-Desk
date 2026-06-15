"""Durable, fenced Qdrant delivery; no active revision changes in this worker."""

from uuid import UUID, uuid4

from evidencedesk.db import index_repository as repository
from evidencedesk.db.session import AuthContext, tenant_transaction
from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.index.artifacts import load_artifact, verify_original
from evidencedesk.index.models import Delivery
from evidencedesk.index.qdrant import IndexWriter
from evidencedesk.jobs.lease import heartbeat
from evidencedesk.observability.stages import instrument
from evidencedesk.storage.s3 import S3Store
from qdrant_client.common.client_exceptions import ResourceExhaustedResponse
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class IndexWorker:
    def __init__(
        self,
        db: AsyncEngine,
        outbox: repository.OutboxRepository,
        store: S3Store,
        writer: IndexWriter,
    ):
        self.db, self.outbox, self.store, self.writer = db, outbox, store, writer

    @instrument("index.write")
    async def run(self, lease: Delivery) -> None:
        # Scope comes only from restricted dispatch, never a browser parameter. There is no user session here.
        ctx = AuthContext(UUID(int=0), lease.workspace_id, "internal", 0, uuid4())

        async def guard() -> None:
            async with tenant_transaction(self.db, ctx) as conn:
                await repository.delivery_state(conn, lease, self.writer.config)

        try:
            async with heartbeat(self.outbox, lease):
                async with tenant_transaction(self.db, ctx) as conn:
                    row = await repository.delivery_state(conn, lease, self.writer.config)
                if lease.operation == "purge":
                    await self.writer.purge(lease.workspace_id, lease.revision_id, guard)
                    async with tenant_transaction(self.db, ctx) as conn:
                        await repository.delivery_state(conn, lease, self.writer.config)
                        await conn.execute(
                            text("""UPDATE app.index_manifests SET state='purged',verified_points=0,
                            verified_at=NULL,updated_at=clock_timestamp() WHERE workspace_id=:w AND revision_id=:r AND profile_id=:p"""),
                            repository.params(lease),
                        )
                        await repository.complete(conn, lease)
                    return
                artifact = await load_artifact(self.store, row)
                await verify_original(self.store, row)
                async with tenant_transaction(self.db, ctx) as conn:
                    await repository.delivery_state(conn, lease, self.writer.config)
                    await repository.check_chunks(conn, artifact)
                    await conn.execute(
                        text("""UPDATE app.index_manifests SET state='indexing',verified_points=0,
                        verified_at=NULL,updated_at=clock_timestamp() WHERE workspace_id=:w AND revision_id=:r AND profile_id=:p"""),
                        repository.params(lease),
                    )
                await self.writer.upsert(artifact, row["manifest_sha256"], guard)
                await self.writer.verify(artifact, row["manifest_sha256"], guard)
                async with tenant_transaction(self.db, ctx) as conn:
                    await repository.check_chunks(conn, artifact)
                    await repository.mark_verified(conn, lease, self.writer.config)
        except LeaseLost:
            raise
        except (
            ResponseHandlingException,
            UnexpectedResponse,
            ResourceExhaustedResponse,
            TimeoutError,
            OSError,
        ) as exc:
            problem = Problem(
                503, "index_unavailable", "The derived index is temporarily unavailable.", True
            )
            async with tenant_transaction(self.db, ctx) as conn:
                await repository.fail(conn, lease, problem)
            raise problem from exc
        except Problem as exc:
            async with tenant_transaction(self.db, ctx) as conn:
                await repository.fail(conn, lease, exc)
            raise

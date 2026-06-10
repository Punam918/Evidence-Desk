from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.db import job_progress_repository as repository
from evidencedesk.db.idempotency import audit, remember, replay
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.knowledge.models import JobRef


class JobProgress(BaseModel):
    id: UUID
    kind: Literal["ingest", "chat", "purge", "summary", "maintenance", "workflow"]
    status: Literal["queued", "running", "retry_wait", "succeeded", "dead", "cancelled"]
    stage: str = Field(max_length=40)
    attempts: int = Field(ge=0, le=3)
    progress: int = Field(ge=0, le=100)
    error_code: str | None
    updated_at: datetime


class JobProgressService:
    def __init__(self, db: AsyncEngine):
        self.db = db

    async def get(self, ctx: AuthContext, job: UUID) -> JobProgress:
        async with authorized_transaction(self.db, ctx) as conn:
            return JobProgress.model_validate(await repository.get(conn, ctx, job))

    async def retry(self, ctx: AuthContext, job: UUID, key: str) -> JobRef:
        async with authorized_transaction(self.db, ctx, "editor", write=True) as conn:
            await repository.get(conn, ctx, job)
            route = f"POST /jobs/{job}/retry"
            cached = await replay(conn, ctx, route, key, {})
            if cached:
                return JobRef.model_validate(cached)
            generation = await repository.retry(conn, ctx, job)
            result = JobRef(job_id=job)
            await remember(conn, ctx, route, key, {}, result.model_dump(mode="json"), 202)
            await audit(conn, ctx, "ingest_job_retried", "job", job, {"generation": generation})
            return result

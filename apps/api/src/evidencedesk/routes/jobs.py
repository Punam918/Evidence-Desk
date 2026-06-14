from typing import cast
from uuid import UUID

from fastapi import APIRouter, Request

from evidencedesk.jobs.progress import JobProgress, JobProgressService
from evidencedesk.knowledge.models import JobRef
from evidencedesk.routes.workspaces import Context

router = APIRouter(prefix="/v1/workspaces")


def service(request: Request) -> JobProgressService:
    return cast(JobProgressService, request.app.state.job_progress)


@router.get("/{workspace_id}/jobs/{job_id}")
async def get(request: Request, ctx: Context, job_id: UUID) -> JobProgress:
    return await service(request).get(ctx, job_id)


@router.post("/{workspace_id}/jobs/{job_id}/retry", status_code=202)
async def retry(request: Request, ctx: Context, job_id: UUID) -> JobRef:
    return await service(request).retry(ctx, job_id, request.headers["idempotency-key"])

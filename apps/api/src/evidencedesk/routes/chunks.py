from typing import cast
from uuid import UUID

from fastapi import APIRouter, Request
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from evidencedesk.evidence.models import Chunk
from evidencedesk.evidence.service import EvidenceService
from evidencedesk.ingestion.upload import UploadService
from evidencedesk.routes.workspaces import Context
from evidencedesk.storage.download import disposition, prepare

router = APIRouter(prefix="/v1/workspaces")


@router.get("/{workspace_id}/chunks/{chunk_id}")
async def chunk(request: Request, ctx: Context, chunk_id: UUID) -> Chunk:
    return await EvidenceService(request.app.state.db).chunk(ctx, chunk_id)


@router.get("/{workspace_id}/documents/{document_id}/download")
async def download(
    request: Request, ctx: Context, document_id: UUID, revision_id: UUID | None = None
) -> StreamingResponse:
    uploads = cast(UploadService, request.app.state.uploads)
    attachment = await prepare(
        EvidenceService(request.app.state.db), uploads.store, ctx, document_id, revision_id
    )
    return StreamingResponse(
        attachment.stream(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": disposition(attachment.source.title),
            "Content-Length": str(attachment.source.size),
            "Cache-Control": "private, no-store",
        },
        background=BackgroundTask(attachment.directory.cleanup),
    )

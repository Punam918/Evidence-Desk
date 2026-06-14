from typing import cast
from uuid import UUID

from fastapi import APIRouter, Request

from evidencedesk.knowledge.models import (
    Document,
    JobRef,
    KnowledgeBase,
    KnowledgeBaseInput,
    Revision,
)
from evidencedesk.knowledge.service import KnowledgeService
from evidencedesk.routes.workspaces import Context, Cursor, Limit
from evidencedesk.tenancy.models import Page

router = APIRouter(prefix="/v1/workspaces")


def service(request: Request) -> KnowledgeService:
    return cast(KnowledgeService, request.app.state.knowledge)


@router.post("/{workspace_id}/knowledge-bases", status_code=201)
async def create(request: Request, ctx: Context, value: KnowledgeBaseInput) -> KnowledgeBase:
    return await service(request).create(ctx, value, request.headers["idempotency-key"])


@router.get("/{workspace_id}/knowledge-bases")
async def list_kbs(
    request: Request, ctx: Context, limit: Limit = 20, cursor: Cursor = None
) -> Page[KnowledgeBase]:
    return await service(request).list(ctx, limit, cursor)


@router.delete("/{workspace_id}/knowledge-bases/{kb_id}", status_code=202)
async def delete(request: Request, ctx: Context, kb_id: UUID) -> JobRef:
    return await service(request).delete(ctx, kb_id)


@router.get("/{workspace_id}/knowledge-bases/{kb_id}/documents")
async def documents(
    request: Request, ctx: Context, kb_id: UUID, limit: Limit = 20, cursor: Cursor = None
) -> Page[Document]:
    return await service(request).documents(ctx, kb_id, limit, cursor)


@router.get("/{workspace_id}/documents/{document_id}")
async def document(request: Request, ctx: Context, document_id: UUID) -> Document:
    return await service(request).document(ctx, document_id)


@router.get("/{workspace_id}/documents/{document_id}/revisions")
async def revisions(
    request: Request, ctx: Context, document_id: UUID, limit: Limit = 20, cursor: Cursor = None
) -> Page[Revision]:
    return await service(request).revisions(ctx, document_id, limit, cursor)

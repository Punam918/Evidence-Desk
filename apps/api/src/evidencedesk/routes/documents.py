"""Multipart transport. Authorization completes before any uploaded bytes are parsed."""

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from evidencedesk.db.upload_slot import UploadSlot
from evidencedesk.errors import Problem
from evidencedesk.ingestion.models import ReprocessInput, UploadAccepted, UploadFileInfo
from evidencedesk.ingestion.upload import UploadService
from evidencedesk.ingestion.validation import MAX_BYTES, display_name, validate_file
from evidencedesk.routes.auth import service as identity_service
from evidencedesk.routes.workspaces import Context

router = APIRouter(prefix="/v1/workspaces")
MAX_BODY = MAX_BYTES + 64 * 1024


class UploadTooLarge(MultiPartException):
    pass


def service(request: Request) -> UploadService:
    return cast(UploadService, request.app.state.uploads)


async def limited_stream(request: Request) -> AsyncGenerator[bytes]:
    total = 0
    async for part in request.stream():
        total += len(part)
        if total > MAX_BODY:
            raise UploadTooLarge("Upload body too large")
        yield part


def copy_file(file: UploadFile, path: Path) -> UploadFileInfo:
    title = display_name(file.filename)
    digest, size = hashlib.sha256(), 0
    with path.open("wb") as target:
        while part := file.file.read(64 * 1024):
            size += len(part)
            if size > MAX_BYTES:
                raise Problem(413, "upload_size", "The file must be at most 20 MiB.")
            digest.update(part)
            target.write(part)
    return UploadFileInfo(
        path, title, validate_file(path, title, file.content_type), size, digest.hexdigest()
    )


async def receive_body(
    request: Request, ctx: Context, kb: UUID | None = None, document: UUID | None = None
) -> UploadAccepted:
    uploads = service(request)
    if kb is None:
        kb = await uploads.preflight(ctx, kb, document, request.headers["idempotency-key"])
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "multipart/form-data"
    ):
        raise Problem(415, "multipart_required", "Upload one file as multipart/form-data.")
    length = request.headers.get("content-length")
    if length:
        try:
            declared = int(length)
            if declared < 0:
                raise ValueError("negative length")
        except ValueError as exc:
            raise Problem(400, "invalid_length", "Invalid request length.") from exc
        if declared > MAX_BODY:
            raise Problem(413, "upload_size", "The upload body exceeds the allowed limit.")
    parser = MultiPartParser(
        request.headers, limited_stream(request), max_files=1, max_fields=0, max_part_size=64 * 1024
    )
    try:
        form = await parser.parse()
    except MultiPartException as exc:
        raise Problem(
            413 if isinstance(exc, UploadTooLarge) else 422,
            "invalid_upload",
            "Upload exactly one file of at most 20 MiB.",
        ) from exc
    try:
        pairs = form.multi_items()
        if len(pairs) != 1 or pairs[0][0] != "file" or not isinstance(pairs[0][1], UploadFile):
            raise Problem(422, "invalid_upload", "Upload exactly one field named file.")
        with TemporaryDirectory(prefix="ed-upload-") as directory:
            info = await asyncio.to_thread(copy_file, pairs[0][1], Path(directory) / "original")
            return await uploads.upload(ctx, kb, document, info, request.headers["idempotency-key"])
    finally:
        await form.close()


async def receive(
    request: Request, ctx: Context, kb: UUID | None = None, document: UUID | None = None
) -> UploadAccepted:
    # Resolve authorization before rate/slot acquisition and before consuming any body bytes.
    kb = await service(request).preflight(ctx, kb, document, request.headers["idempotency-key"])
    allowed = await identity_service(request).repository.consume_rate(ctx.user_id, "upload", 10)
    if not allowed:
        raise Problem(429, "upload_rate", "Too many uploads. Try again next minute.", True)
    slot = await UploadSlot.acquire(request.app.state.settings.database_url, ctx.user_id)
    try:
        return await receive_body(request, ctx, kb, document)
    finally:
        await slot.close()


@router.post("/{workspace_id}/knowledge-bases/{kb_id}/documents", status_code=202)
async def upload_document(request: Request, ctx: Context, kb_id: UUID) -> UploadAccepted:
    return await receive(request, ctx, kb=kb_id)


@router.post("/{workspace_id}/documents/{document_id}/revisions", status_code=202)
async def upload_revision(request: Request, ctx: Context, document_id: UUID) -> UploadAccepted:
    return await receive(request, ctx, document=document_id)


@router.post("/{workspace_id}/documents/{document_id}/reprocess", status_code=202)
async def reprocess(
    request: Request, ctx: Context, document_id: UUID, value: ReprocessInput
) -> UploadAccepted:
    return await service(request).reprocess(
        ctx, document_id, value, request.headers["idempotency-key"]
    )

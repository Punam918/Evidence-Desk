from typing import cast
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from evidencedesk.chat.models import Conversation, ConversationInput, Message, Run, RunInput
from evidencedesk.chat.service import ChatService
from evidencedesk.db.stream_slots import StreamSlot
from evidencedesk.events.stream import event_cursor, stream_events
from evidencedesk.routes.auth import service
from evidencedesk.routes.workspaces import Context, Cursor, Limit
from evidencedesk.tenancy.models import Page

router = APIRouter(prefix="/v1/workspaces/{workspace_id}")


def chat(request: Request) -> ChatService:
    return cast(ChatService, request.app.state.chat)


@router.post("/conversations", status_code=201)
async def create(request: Request, ctx: Context, value: ConversationInput) -> Conversation:
    return await chat(request).create(ctx, value, request.headers["idempotency-key"])


@router.get("/conversations")
async def conversations(
    request: Request, ctx: Context, limit: Limit = 20, cursor: Cursor = None
) -> Page[Conversation]:
    return await chat(request).list_conversations(ctx, limit, cursor)


@router.get("/conversations/{conversation_id}")
async def conversation(request: Request, ctx: Context, conversation_id: UUID) -> Conversation:
    return await chat(request).get(ctx, conversation_id)


@router.delete("/conversations/{conversation_id}", status_code=202)
async def delete(request: Request, ctx: Context, conversation_id: UUID) -> dict[str, str]:
    return await chat(request).delete(ctx, conversation_id)


@router.get("/conversations/{conversation_id}/messages")
async def messages(
    request: Request, ctx: Context, conversation_id: UUID, limit: Limit = 20, cursor: Cursor = None
) -> Page[Message]:
    return await chat(request).messages(ctx, conversation_id, limit, cursor)


@router.get("/runs/{run_id}")
async def run(request: Request, ctx: Context, run_id: UUID) -> Run:
    return await chat(request).run(ctx, run_id)


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel(request: Request, ctx: Context, run_id: UUID) -> Run:
    return await chat(request).cancel(ctx, run_id, request.headers["idempotency-key"])


@router.get("/runs/{run_id}/events")
async def events(request: Request, ctx: Context, run_id: UUID) -> StreamingResponse:
    after = event_cursor(request.headers.get("last-event-id"))
    await chat(request).event_batch(ctx, run_id, after)
    identity = service(request)
    slot = await StreamSlot.acquire(identity.settings.identity_database_url, ctx.user_id)
    body = stream_events(
        identity,
        chat(request),
        slot,
        request.cookies["ed_session"],
        ctx.workspace_id,
        run_id,
        ctx.request_id,
        after,
        request.is_disconnected,
    )
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/conversations/{conversation_id}/runs", status_code=202)
async def start_run(request: Request, ctx: Context, conversation_id: UUID, value: RunInput) -> Run:
    return await chat(request).start_run(
        ctx, conversation_id, value, request.headers["idempotency-key"]
    )

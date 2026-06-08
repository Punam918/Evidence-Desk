import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

import psycopg

from evidencedesk.chat.models import RunEvent
from evidencedesk.chat.service import ChatService
from evidencedesk.db.stream_slots import StreamSlot
from evidencedesk.errors import Problem
from evidencedesk.identity.service import IdentityService

TERMINAL = {"answer.completed", "run.failed", "run.cancelled", "run.refused"}


def encode(event: RunEvent) -> bytes:
    return f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n".encode()


def event_cursor(value: str | None) -> int:
    if value is None:
        return 0
    if not value.isascii() or not value.isdigit() or len(value) > 18:
        raise Problem(409, "invalid_event_cursor", "Last-Event-ID must be a nonnegative integer.")
    return int(value)


async def stream_events(
    identity: IdentityService,
    chat: ChatService,
    slot: StreamSlot,
    session: str,
    workspace: UUID,
    run: UUID,
    request_id: UUID,
    after: int,
    disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[bytes]:
    started = last_ping = time.monotonic()
    try:
        while time.monotonic() - started < 65 and not await disconnected():
            await slot.check()
            actor = await identity.authenticate(session)
            ctx = await identity.context(actor, workspace, request_id)
            events, terminal = await chat.event_batch(ctx, run, after)
            if events:
                # One committed event per fresh authorization, including before final replay.
                event = events[0]
                yield encode(event)
                after = event.seq
                if event.type in TERMINAL:
                    return
                continue
            if terminal:
                return
            if time.monotonic() - last_ping >= 15:
                yield b": ping\n\n"
                last_ping = time.monotonic()
            await asyncio.sleep(0.5)
    except Problem, psycopg.Error:
        # Headers are already sent. Close; reconnect obtains a sanitized HTTP error.
        return
    finally:
        await asyncio.shield(slot.close())

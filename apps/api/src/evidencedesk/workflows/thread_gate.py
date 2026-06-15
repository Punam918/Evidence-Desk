"""One graph invocation per thread even when a business lease is reclaimed."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import UUID

import psycopg

from evidencedesk.errors import Problem


@asynccontextmanager
async def thread_gate(dsn: str, thread: UUID, interval: float = 5) -> AsyncIterator[None]:
    key = int.from_bytes(hashlib.sha256(thread.bytes).digest()[:8], "big", signed=True)
    parent = asyncio.current_task()
    lost = False
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        result = await (await conn.execute("SELECT pg_try_advisory_lock(%s)", (key,))).fetchone()
        if not result or not result[0]:
            raise Problem(409, "workflow_busy", "This workflow is already executing.", True)

        async def monitor() -> None:
            nonlocal lost
            while True:
                await asyncio.sleep(interval)
                try:
                    await conn.execute("SELECT 1")
                except psycopg.Error:
                    lost = True
                    if parent:
                        parent.cancel()
                    return

        task = asyncio.create_task(monitor())
        try:
            yield
        except asyncio.CancelledError:
            if lost:
                raise Problem(409, "workflow_gate_lost", "Workflow ownership was lost.") from None
            raise
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if not conn.closed:
                with suppress(psycopg.Error):
                    await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))

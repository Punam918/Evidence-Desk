"""One local CPU generation slot across worker processes, held outside transactions."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.errors import Problem


@asynccontextmanager
async def local_generation_slot(db: AsyncEngine, *, slot_id: int = 1) -> AsyncIterator[None]:
    parent = asyncio.current_task()
    lost = False
    async with db.connect() as pooled:
        conn = await pooled.execution_options(isolation_level="AUTOCOMMIT")
        acquired = (
            await conn.execute(text("SELECT pg_try_advisory_lock(17734,:slot)"), {"slot": slot_id})
        ).scalar_one()
        if not acquired:
            raise Problem(429, "local_model_busy", "Local inference is at capacity.", True)

        async def monitor() -> None:
            nonlocal lost
            while True:
                await asyncio.sleep(1)
                try:
                    await conn.execute(text("SELECT 1"))
                except SQLAlchemyError:
                    lost = True
                    if parent:
                        parent.cancel()
                    return

        task = asyncio.create_task(monitor())
        try:
            yield
        except asyncio.CancelledError:
            if lost:
                raise Problem(
                    409, "local_slot_lost", "Local inference ownership was lost."
                ) from None
            raise
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            try:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(17734,:slot)"), {"slot": slot_id}
                )
            except SQLAlchemyError:
                await conn.invalidate()

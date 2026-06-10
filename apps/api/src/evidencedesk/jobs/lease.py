"""Heartbeat loss cancels the current stage; each mutation also checks its fencing token."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Protocol

from evidencedesk.errors import LeaseLost


class Renewer[T](Protocol):
    async def renew(self, lease: T) -> None: ...


@asynccontextmanager
async def heartbeat[T](
    repository: Renewer[T], lease: T, interval: float = 10
) -> AsyncIterator[None]:
    parent = asyncio.current_task()
    lost = False

    async def renew() -> None:
        nonlocal lost
        while True:
            await asyncio.sleep(interval)
            try:
                await repository.renew(lease)
            except Exception:
                lost = True
                if parent:
                    parent.cancel()
                return

    task = asyncio.create_task(renew())
    try:
        yield
    except asyncio.CancelledError:
        if lost:
            raise LeaseLost() from None
        raise
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

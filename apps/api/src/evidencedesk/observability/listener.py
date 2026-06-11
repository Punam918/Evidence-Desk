"""Separate authenticated scrape socket; never mounted on the public application router."""

import asyncio
import hmac
from collections.abc import Callable


class MetricsListener:
    def __init__(self, scrape: Callable[[], bytes], token: bytes):
        self.scrape, self.token = scrape, token
        self.server: asyncio.Server | None = None
        self.active = 0
        self.tasks: set[asyncio.Task[None]] = set()

    async def start(self, host: str, port: int) -> None:
        self.server = await asyncio.start_server(self.handle, host, port, limit=8192, backlog=16)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:
            self.tasks.add(task)
        try:
            if self.active >= 8:
                return
            self.active += 1
            try:
                async with asyncio.timeout(2):
                    data = await reader.readuntil(b"\r\n\r\n")
                    if len(data) > 8192:
                        return
                    lines = data.split(b"\r\n")
                    credentials = [
                        line.split(b":", 1)[1].strip()
                        for line in lines[1:]
                        if line.lower().startswith(b"authorization:")
                    ]
                    if len(credentials) != 1 or not hmac.compare_digest(
                        credentials[0], b"Bearer " + self.token
                    ):
                        code, body = b"401 Unauthorized", b"Unauthorized\n"
                    elif lines[0] not in (b"GET /metrics HTTP/1.1", b"GET /metrics HTTP/1.0"):
                        code, body = b"404 Not Found", b"Not found\n"
                    else:
                        code, body = b"200 OK", self.scrape()
                        if len(body) > 2 * 1024 * 1024:
                            code, body = b"503 Service Unavailable", b"Unavailable\n"
                    writer.write(
                        b"HTTP/1.1 "
                        + code
                        + b"\r\nContent-Type: text/plain; version=0.0.4; charset=utf-8\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\n\r\n"
                        + body
                    )
                    await writer.drain()
            finally:
                self.active -= 1
        except (
            TimeoutError,
            ConnectionError,
            ValueError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            if task:
                self.tasks.discard(task)

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

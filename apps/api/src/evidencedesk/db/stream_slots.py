"""Session locks bound stream readers across API processes without open transactions."""

import hashlib
from uuid import UUID

import psycopg

from evidencedesk.errors import Problem


class StreamSlot:
    def __init__(self, connection: psycopg.AsyncConnection[tuple[object, ...]]):
        self.connection = connection

    @staticmethod
    def key(value: str) -> int:
        return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big", signed=True)

    @classmethod
    async def acquire(cls, dsn: str, user: UUID) -> StreamSlot:
        conn = await psycopg.AsyncConnection.connect(
            dsn.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True
        )
        try:
            for category, count in [("global", 10), (str(user), 3)]:
                for slot in range(count):
                    result = await (
                        await conn.execute(
                            "SELECT pg_try_advisory_lock(%s)",
                            (cls.key(f"ed-stream:{category}:{slot}"),),
                        )
                    ).fetchone()
                    if result and result[0]:
                        break
                else:
                    raise Problem(429, "stream_limit", "Too many simultaneous event readers.", True)
            return cls(conn)
        except BaseException:
            await conn.close()
            raise

    async def check(self) -> None:
        await self.connection.execute("SELECT 1")

    async def close(self) -> None:
        await self.connection.close()

"""One upload per actor across API processes, without an open transaction."""

from uuid import UUID

import psycopg

from evidencedesk.db.stream_slots import StreamSlot
from evidencedesk.errors import Problem


class UploadSlot(StreamSlot):
    @classmethod
    async def acquire(cls, dsn: str, user: UUID) -> UploadSlot:
        conn = await psycopg.AsyncConnection.connect(
            dsn.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True
        )
        try:
            row = await (
                await conn.execute(
                    "SELECT pg_try_advisory_lock(%s)", (cls.key(f"ed-upload:{user}"),)
                )
            ).fetchone()
            if not row or not row[0]:
                raise Problem(
                    429,
                    "upload_concurrency",
                    "Another upload is in progress for your account.",
                    True,
                )
            return cls(conn)
        except BaseException:
            await conn.close()
            raise

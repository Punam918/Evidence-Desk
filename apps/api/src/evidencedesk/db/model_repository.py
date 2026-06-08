from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def health(conn: AsyncConnection) -> dict[str, dict[str, Any]]:
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT *,last_probe_at>clock_timestamp()-interval '5 minutes' AS fresh FROM app.provider_health"
                )
            )
        )
        .mappings()
        .all()
    )
    return {row["model_key"]: dict(row) for row in rows}

from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def snapshot(db: AsyncEngine) -> list[dict[str, Any]]:
    async with db.connect() as conn:
        return cast(
            list[dict[str, Any]],
            (await conn.execute(text("SELECT app.operational_metrics()"))).scalar_one(),
        )

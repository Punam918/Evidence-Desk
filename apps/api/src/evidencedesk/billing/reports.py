"""Content-free ledger aggregates. Stored attempt prices, not today's registry, determine cost."""

from datetime import date
from typing import Literal

from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.errors import Problem
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class UsageBucket(BaseModel):
    day: date
    model_key: str
    requests: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_micro_usd: int = Field(ge=0)
    estimated_attempts: int = Field(ge=0)


class Usage(BaseModel):
    buckets: list[UsageBucket]
    reserved_micro_usd: int = Field(ge=0)
    currency: Literal["USD"] = "USD"


class UsageReports:
    def __init__(self, db: AsyncEngine):
        self.db = db

    async def get(self, ctx: AuthContext, start: date, end: date) -> Usage:
        if not 0 <= (end - start).days < 90:
            raise Problem(
                422, "invalid_date_window", "Choose an inclusive UTC date range of 1–90 days."
            )
        async with authorized_transaction(self.db, ctx) as conn:
            # Do not trust the role from a session or a previously resolved request context.
            role = (
                await conn.execute(
                    text("SELECT role FROM app.memberships WHERE workspace_id=:w AND user_id=:u"),
                    {"w": ctx.workspace_id, "u": ctx.user_id},
                )
            ).scalar_one()
            predicate = "workspace_id=:w AND day_start BETWEEN :start AND :end"
            if role not in ("admin", "owner"):
                predicate += " AND user_id=:u"
            params = {"w": ctx.workspace_id, "u": ctx.user_id, "start": start, "end": end}
            rows = (
                (
                    await conn.execute(
                        text(
                            """SELECT day_start AS day,model_key,count(*) AS requests,
                coalesce(sum(input_tokens),0) AS input_tokens,coalesce(sum(output_tokens),0) AS output_tokens,
                coalesce(sum(cost_micro_usd) FILTER (WHERE state<>'reserved'),0) AS cost_micro_usd,
                count(*) FILTER (WHERE usage_estimated OR state='estimated') AS estimated_attempts
                FROM app.model_usage WHERE """
                            + predicate
                            + " GROUP BY day_start,model_key ORDER BY day_start,model_key LIMIT 1001"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            if len(rows) > 1000:
                raise Problem(422, "usage_window_too_large", "Choose a smaller date range.")
            reserved = (
                await conn.execute(
                    text(
                        "SELECT coalesce(sum(reserved_micro_usd),0) FROM app.model_usage WHERE "
                        + predicate
                        + " AND state='reserved'"
                    ),
                    params,
                )
            ).scalar_one()
            return Usage(
                buckets=[UsageBucket.model_validate(row) for row in rows],
                reserved_micro_usd=reserved,
            )

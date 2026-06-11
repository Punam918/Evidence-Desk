import asyncio
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.db.health_repository import signals
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.errors import Problem
from evidencedesk.models.catalog import ModelCatalog


class Check(BaseModel):
    name: str
    status: Literal["ok", "degraded", "unavailable"]
    detail_code: str


class Health(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    checks: list[Check]


class HealthService:
    def __init__(self, db: AsyncEngine, models: ModelCatalog):
        self.db, self.models = db, models

    async def get(self, ctx: AuthContext) -> Health:
        try:
            async with asyncio.timeout(2):
                async with authorized_transaction(self.db, ctx, "admin") as conn:
                    current = await signals(conn, ctx)
                    eligible = await self.models.availability(conn, ctx)
        except (TimeoutError, SQLAlchemyError) as exc:
            raise Problem(
                503, "health_unavailable", "Operational health could not be read.", True
            ) from exc
        checks = [Check(name="database", status="ok", detail_code="scoped_read_succeeded")]
        checks.extend(
            Check(
                name=name,
                status="degraded" if value else "ok",
                detail_code="attention_required" if value else "no_recorded_issue",
            )
            for name, value in current.items()
        )
        ready = any(model.available for model in eligible)
        checks.append(
            Check(
                name="generation",
                status="ok" if ready else "degraded",
                detail_code="eligible_probed_model" if ready else "no_eligible_probed_model",
            )
        )
        # The API does not query remote dependencies here or spend money on inference probes.
        # Index fields describe recorded reconciliation state, never a live Qdrant availability claim.
        return Health(
            status="degraded" if any(c.status != "ok" for c in checks) else "ok", checks=checks
        )

"""Bounded, aggregate-only reports; an import is an administrator assertion, not certification."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.db import evaluation_repository as repo
from evidencedesk.db import idempotency
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.errors import Problem
from evidencedesk.pagination import Cursors
from evidencedesk.tenancy.models import Input, Page

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Label = Annotated[
    str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/+ -]*$")
]


class Metric(Input):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    name: Label
    value: float | None
    n: int = Field(ge=0)
    ci_low: float | None
    ci_high: float | None

    @model_validator(mode="after")
    def coherent(self) -> Metric:
        if (self.ci_low is None) != (self.ci_high is None):
            raise ValueError("Both interval bounds are required")
        if self.value is None:
            if self.n != 0 or self.ci_low is not None:
                raise ValueError("Unmeasured metrics must have n=0 and no interval")
        elif self.n == 0:
            raise ValueError("Measured metrics need a denominator")
        if self.ci_low is not None and self.ci_high is not None and self.ci_low > self.ci_high:
            raise ValueError("Interval bounds are reversed")
        return self


class Aggregate(Input):
    dataset_version: str = Field(
        min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    dataset_sha256: Hash
    config_sha256: Hash
    git_sha: str | None = Field(pattern=r"^[a-f0-9]{40}$")
    variant: Label
    model_key: Label
    hardware: str = Field(
        min_length=1, max_length=300, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/+ ()-]*$"
    )
    status: Literal["completed", "failed"]
    case_count: int = Field(ge=1)
    metrics: list[Metric] = Field(max_length=100)
    artifact_sha256: Hash

    @model_validator(mode="after")
    def unique_metrics(self) -> Aggregate:
        if len({m.name for m in self.metrics}) != len(self.metrics):
            raise ValueError("Metric names must be unique")
        return self


class EvaluationInput(Aggregate):
    source_snapshot_sha256: Hash
    evidence_scope: Literal["draft_diagnostic", "reviewed_benchmark", "operational"]


class Evaluation(Aggregate):
    source_snapshot_sha256: Hash | None
    evidence_scope: Literal[
        "draft_diagnostic", "reviewed_benchmark", "operational", "legacy_unclassified"
    ]
    id: UUID
    created_at: datetime


class Evaluations:
    def __init__(self, db: AsyncEngine, cursors: Cursors):
        self.db, self.cursors = db, cursors

    async def create(self, ctx: AuthContext, value: EvaluationInput, key: str) -> Evaluation:
        async with authorized_transaction(self.db, ctx, "admin", write=True) as conn:
            body = value.model_dump(mode="json")
            old = await idempotency.replay(conn, ctx, "evaluation-import", key, body)
            if old:
                return Evaluation.model_validate(await repo.get(conn, ctx, UUID(old["id"])))
            row = await repo.insert(conn, ctx, body)
            await idempotency.remember(
                conn, ctx, "evaluation-import", key, body, {"id": str(row["id"])}, 201
            )
            await idempotency.audit(
                conn,
                ctx,
                "evaluation.imported",
                "evaluation",
                row["id"],
                {
                    "artifact_sha256": value.artifact_sha256,
                    "evidence_scope": value.evidence_scope,
                },
            )
            return Evaluation.model_validate(row)

    async def get(self, ctx: AuthContext, uid: UUID) -> Evaluation:
        async with authorized_transaction(self.db, ctx, "admin") as conn:
            return Evaluation.model_validate(await repo.get(conn, ctx, uid))

    async def list(self, ctx: AuthContext, limit: int, cursor: str | None) -> Page[Evaluation]:
        scope = {
            "kind": "evaluations",
            "workspace": str(ctx.workspace_id),
            "user": str(ctx.user_id),
        }
        position = self.cursors.decode(cursor, scope)
        try:
            after = datetime.fromisoformat(position[0]) if position else None
            last = UUID(position[1]) if position else None
            if position and (len(position) != 2 or after is None or after.tzinfo is None):
                raise ValueError("Invalid cursor")
        except (ValueError, IndexError) as exc:
            raise Problem(422, "invalid_cursor", "Invalid page cursor.") from exc
        async with authorized_transaction(self.db, ctx, "admin") as conn:
            rows = await repo.page(conn, ctx, after, last, limit + 1)
            next_cursor = (
                self.cursors.encode(
                    scope, [rows[limit - 1]["created_at"].isoformat(), str(rows[limit - 1]["id"])]
                )
                if len(rows) > limit
                else None
            )
            return Page[Evaluation](
                items=[Evaluation.model_validate(r) for r in rows[:limit]], next_cursor=next_cursor
            )

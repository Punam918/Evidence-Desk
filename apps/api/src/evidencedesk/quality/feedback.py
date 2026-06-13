"""Private, naturally idempotent feedback. Never exports content or trains a model."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.db.chat_repository import run_sources_live
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.errors import Problem
from evidencedesk.tenancy.models import Input


class FeedbackInput(Input):
    vote: Literal[-1, 1]
    reason: str = Field(default="", max_length=1000)

    @field_validator("vote", mode="before")
    @classmethod
    def integer_vote(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Vote must be an integer")
        return value


class Feedback(FeedbackInput):
    message_id: UUID
    updated_at: datetime


class FeedbackService:
    def __init__(self, db: AsyncEngine):
        self.db = db

    async def put(self, ctx: AuthContext, message: UUID, value: FeedbackInput) -> Feedback:
        async with authorized_transaction(self.db, ctx, write=True) as conn:
            run = (
                await conn.execute(
                    text("""SELECT r.id FROM app.messages m
                JOIN app.conversations c ON c.workspace_id=m.workspace_id AND c.id=m.conversation_id
                JOIN app.runs r ON r.workspace_id=m.workspace_id AND r.assistant_message_id=m.id AND r.user_id=m.user_id
                WHERE m.workspace_id=:w AND m.id=:m AND m.user_id=:u AND c.user_id=:u AND c.deleted_at IS NULL
                AND m.role='assistant' AND m.state='final' AND r.status='completed'"""),
                    {"w": ctx.workspace_id, "m": message, "u": ctx.user_id},
                )
            ).scalar_one_or_none()
            if run is None or not await run_sources_live(conn, ctx, run):
                raise Problem(404, "not_found", "This answer is unavailable for feedback.")
            row = (
                (
                    await conn.execute(
                        text("""INSERT INTO app.feedback(workspace_id,message_id,user_id,vote,reason)
                VALUES(:w,:m,:u,:v,:reason) ON CONFLICT(workspace_id,message_id,user_id) DO UPDATE
                SET vote=excluded.vote,reason=excluded.reason,
                updated_at=CASE WHEN (app.feedback.vote,app.feedback.reason) IS DISTINCT FROM (excluded.vote,excluded.reason)
                THEN now() ELSE app.feedback.updated_at END
                RETURNING message_id,vote,reason,updated_at"""),
                        {
                            "w": ctx.workspace_id,
                            "m": message,
                            "u": ctx.user_id,
                            "v": value.vote,
                            "reason": value.reason,
                        },
                    )
                )
                .mappings()
                .one()
            )
            return Feedback.model_validate(dict(row))

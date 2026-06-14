from datetime import date
from uuid import UUID

from fastapi import APIRouter, Request

from evidencedesk.billing.reports import Usage, UsageReports
from evidencedesk.quality.feedback import Feedback, FeedbackInput, FeedbackService
from evidencedesk.routes.workspaces import Context

router = APIRouter(prefix="/v1/workspaces")


@router.put("/{workspace_id}/messages/{message_id}/feedback")
async def feedback(
    request: Request, ctx: Context, message_id: UUID, value: FeedbackInput
) -> Feedback:
    return await FeedbackService(request.app.state.db).put(ctx, message_id, value)


@router.get("/{workspace_id}/usage")
async def usage(request: Request, ctx: Context, start_date: date, end_date: date) -> Usage:
    return await UsageReports(request.app.state.db).get(ctx, start_date, end_date)

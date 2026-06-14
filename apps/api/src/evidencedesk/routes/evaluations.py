from uuid import UUID

from fastapi import APIRouter, Request, Response

from evidencedesk.quality.evaluations import Evaluation, EvaluationInput, Evaluations
from evidencedesk.routes.workspaces import Context, Cursor, Limit
from evidencedesk.tenancy.models import Page

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/evaluations")


def service(request: Request) -> Evaluations:
    return Evaluations(request.app.state.db, request.app.state.tenancy.cursors)


@router.post("", status_code=201)
async def create(
    request: Request, ctx: Context, value: EvaluationInput, response: Response
) -> Evaluation:
    result = await service(request).create(ctx, value, request.headers["idempotency-key"])
    response.headers["Location"] = f"/v1/workspaces/{ctx.workspace_id}/evaluations/{result.id}"
    return result


@router.get("")
async def list_reports(
    request: Request, ctx: Context, limit: Limit = 20, cursor: Cursor = None
) -> Page[Evaluation]:
    return await service(request).list(ctx, limit, cursor)


@router.get("/{evaluation_id}")
async def get(request: Request, ctx: Context, evaluation_id: UUID) -> Evaluation:
    return await service(request).get(ctx, evaluation_id)

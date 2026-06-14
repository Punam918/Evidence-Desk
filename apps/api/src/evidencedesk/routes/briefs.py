from uuid import UUID

from fastapi import APIRouter, Request, Response

from evidencedesk.routes.workspaces import Context, Cursor, Limit
from evidencedesk.tenancy.models import Page
from evidencedesk.tools.models import ApprovalInput, Brief, BriefProposalInput, ToolExecution
from evidencedesk.tools.service import BriefService

router = APIRouter(prefix="/v1/workspaces/{workspace_id}")


def service(request: Request) -> BriefService:
    return BriefService(request.app.state.db, request.app.state.tenancy.cursors)


@router.post("/runs/{run_id}/brief-proposals", status_code=201)
async def propose(
    request: Request, ctx: Context, run_id: UUID, value: BriefProposalInput, response: Response
) -> ToolExecution:
    result = await service(request).propose(ctx, run_id, value, request.headers["idempotency-key"])
    response.headers["Location"] = f"/v1/workspaces/{ctx.workspace_id}/tool-executions/{result.id}"
    return result


@router.get("/tool-executions/{execution_id}")
async def execution(request: Request, ctx: Context, execution_id: UUID) -> ToolExecution:
    return await service(request).get(ctx, execution_id)


@router.post("/tool-executions/{execution_id}/approval")
async def approve(
    request: Request, ctx: Context, execution_id: UUID, value: ApprovalInput, response: Response
) -> ToolExecution:
    response.headers["Location"] = (
        f"/v1/workspaces/{ctx.workspace_id}/tool-executions/{execution_id}"
    )
    return await service(request).decide(
        ctx, execution_id, value, request.headers["idempotency-key"]
    )


@router.get("/runs/{run_id}/tool-executions")
async def executions(
    request: Request, ctx: Context, run_id: UUID, limit: Limit = 20, cursor: Cursor = None
) -> Page[ToolExecution] | Page[Brief]:
    return await service(request).list(ctx, limit, cursor, run_id)


@router.get("/briefs")
async def briefs(
    request: Request, ctx: Context, limit: Limit = 20, cursor: Cursor = None
) -> Page[Brief] | Page[ToolExecution]:
    return await service(request).list(ctx, limit, cursor)


@router.get("/briefs/{brief_id}")
async def brief(request: Request, ctx: Context, brief_id: UUID) -> Brief:
    return await service(request).get_brief(ctx, brief_id)


@router.delete("/briefs/{brief_id}", status_code=204)
async def delete(request: Request, ctx: Context, brief_id: UUID) -> Response:
    await service(request).delete(ctx, brief_id)
    return Response(status_code=204)

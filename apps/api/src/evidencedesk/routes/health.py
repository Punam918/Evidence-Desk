from fastapi import APIRouter, Request

from evidencedesk.observability.health import Health, HealthService
from evidencedesk.routes.workspaces import Context

router = APIRouter(prefix="/v1/workspaces")


@router.get("/{workspace_id}/health")
async def workspace_health(request: Request, ctx: Context) -> Health:
    return await HealthService(request.app.state.db, request.app.state.models).get(ctx)

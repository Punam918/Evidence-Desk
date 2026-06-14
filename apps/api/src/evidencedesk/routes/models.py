from typing import cast

from fastapi import APIRouter, Request

from evidencedesk.models.registry import ModelAvailability
from evidencedesk.models.service import ModelService
from evidencedesk.routes.workspaces import Context, Cursor, Limit
from evidencedesk.tenancy.models import Page

router = APIRouter(prefix="/v1/workspaces")


@router.get("/{workspace_id}/models")
async def list_models(
    request: Request, ctx: Context, limit: Limit = 20, cursor: Cursor = None
) -> Page[ModelAvailability]:
    return await cast(ModelService, request.app.state.models).list(ctx, limit, cursor)

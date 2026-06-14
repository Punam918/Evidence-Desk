from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from evidencedesk.db.session import AuthContext
from evidencedesk.identity.service import Identity
from evidencedesk.routes.auth import current_user, service
from evidencedesk.tenancy.models import (
    Member,
    MemberInput,
    Page,
    Policy,
    RoleInput,
    SettingsInput,
    Workspace,
    WorkspaceInput,
)
from evidencedesk.tenancy.service import TenancyService

router = APIRouter(prefix="/v1/workspaces")
User = Annotated[Identity, Depends(current_user)]
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=1024)]


def tenancy(request: Request) -> TenancyService:
    return cast(TenancyService, request.app.state.tenancy)


async def context(request: Request, workspace_id: UUID, identity: User) -> AuthContext:
    return await service(request).context(identity, workspace_id, request.state.request_id)


Context = Annotated[AuthContext, Depends(context)]


@router.post("", status_code=201)
async def create(request: Request, identity: User, value: WorkspaceInput) -> Workspace:
    return await tenancy(request).create(identity, value, request.headers["idempotency-key"])


@router.get("")
async def list_workspaces(
    request: Request, identity: User, limit: Limit = 20, cursor: Cursor = None
) -> Page[Workspace]:
    return await tenancy(request).list(identity, limit, cursor)


@router.get("/{workspace_id}")
async def get(request: Request, ctx: Context) -> Workspace:
    return await tenancy(request).get(ctx)


@router.get("/{workspace_id}/members")
async def members(
    request: Request, ctx: Context, limit: Limit = 20, cursor: Cursor = None
) -> Page[Member]:
    return await tenancy(request).members(ctx, limit, cursor)


@router.post("/{workspace_id}/members", status_code=201)
async def add_member(request: Request, ctx: Context, value: MemberInput) -> Member:
    return await tenancy(request).add_member(ctx, value, request.headers["idempotency-key"])


@router.patch("/{workspace_id}/members/{user_id}")
async def change_member(
    request: Request, ctx: Context, user_id: UUID, value: RoleInput
) -> Member | None:
    return await tenancy(request).change_member(ctx, user_id, value)


@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
async def remove_member(request: Request, ctx: Context, user_id: UUID) -> Response:
    await tenancy(request).change_member(ctx, user_id, None)
    return Response(status_code=204)


@router.get("/{workspace_id}/settings")
async def settings(request: Request, ctx: Context) -> Policy:
    return await tenancy(request).policy(ctx)


@router.patch("/{workspace_id}/settings")
async def change_settings(request: Request, ctx: Context, value: SettingsInput) -> Policy:
    return await tenancy(request).policy(ctx, value)


@router.delete("/{workspace_id}", status_code=202)
async def delete_workspace(request: Request, ctx: Context) -> dict[str, str]:
    return await tenancy(request).delete(ctx)

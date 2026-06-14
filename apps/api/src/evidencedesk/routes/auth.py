from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse

from evidencedesk.errors import Problem
from evidencedesk.identity.service import Identity, IdentityService

router = APIRouter()


def service(request: Request) -> IdentityService:
    return cast(IdentityService, request.app.state.identity)


async def current_user(request: Request) -> Identity:
    identity = await service(request).authenticate(request.cookies.get("ed_session"))
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        service(request).mutation(
            identity, request.headers.get("origin"), request.headers.get("x-csrf-token")
        )
    if request.method == "POST":
        key = request.headers.get("idempotency-key", "")
        if not 16 <= len(key) <= 128:
            raise Problem(
                422, "idempotency_key_required", "Provide a 16–128 character Idempotency-Key."
            )
    await service(request).rate(identity, request.method not in ("GET", "HEAD", "OPTIONS"))
    return identity


@router.get("/auth/login")
async def login(
    request: Request, return_to: Annotated[str, Query(max_length=200)] = "/"
) -> Response:
    url, cookie = await service(request).login(return_to)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        "ed_login", cookie, max_age=300, secure=True, httponly=True, samesite="lax", path="/auth"
    )
    return response


@router.get("/auth/callback")
async def callback(
    request: Request,
    code: Annotated[str, Query(max_length=4096)],
    state: Annotated[str, Query(max_length=4096)],
) -> Response:
    session, path = await service(request).callback(
        code, state, request.cookies.get("ed_login"), request.state.request_id
    )
    response = RedirectResponse(path, status_code=302)
    response.set_cookie(
        "ed_session", session, max_age=86400, secure=True, httponly=True, samesite="lax", path="/"
    )
    response.delete_cookie("ed_login", path="/auth", secure=True, httponly=True, samesite="lax")
    return response


@router.get("/v1/me")
async def me(identity: Annotated[Identity, Depends(current_user)]) -> dict[str, object]:
    return {
        "user": {
            "id": str(identity.user_id),
            "email": identity.email,
            "display_name": identity.display_name,
        },
        "csrf_token": identity.csrf,
        "expires_at": identity.expires_at.isoformat(),
    }


@router.post("/auth/logout", status_code=204)
async def logout(
    request: Request, identity: Annotated[Identity, Depends(current_user)]
) -> Response:
    await service(request).logout(
        identity, request.headers["idempotency-key"], request.state.request_id
    )
    response = Response(status_code=204)
    response.delete_cookie("ed_session", path="/", secure=True, httponly=True, samesite="lax")
    return response

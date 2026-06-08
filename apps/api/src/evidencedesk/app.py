"""ASGI composition root. Public responses never serialize internal exceptions."""

import asyncio
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException

from evidencedesk.chat.service import ChatService
from evidencedesk.config import Settings
from evidencedesk.db.session import engine
from evidencedesk.errors import Problem
from evidencedesk.identity.oidc import OIDC
from evidencedesk.identity.service import IdentityService
from evidencedesk.ingestion.upload import UploadService
from evidencedesk.jobs.progress import JobProgressService
from evidencedesk.knowledge.service import KnowledgeService
from evidencedesk.models.service import ModelService
from evidencedesk.observability.runtime import configured
from evidencedesk.observability.telemetry import HTTPMetricsMiddleware
from evidencedesk.routes import (
    auth,
    briefs,
    chat,
    chunks,
    documents,
    evaluations,
    health,
    jobs,
    knowledge,
    models,
    reports,
    workspaces,
)
from evidencedesk.storage.s3 import S3Store
from evidencedesk.tenancy.service import TenancyService


def create_app(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    config = settings or Settings()  # type: ignore[call-arg]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = engine(config.database_url)
        app.state.identity_db = engine(config.identity_database_url)
        store = None
        if config.s3_endpoint:
            assert config.s3_bucket and config.s3_access_key_file and config.s3_secret_key_file
            store = S3Store(
                config.s3_endpoint,
                config.s3_region,
                config.s3_bucket,
                config.s3_access_key_file.read_text().strip(),
                config.s3_secret_key_file.read_text().strip(),
            )
        app.state.uploads = UploadService(app.state.db, store)
        async with httpx.AsyncClient(
            timeout=10,
            follow_redirects=False,
            transport=transport,
            verify=ssl.create_default_context(
                cafile=str(config.tls_ca_file) if config.tls_ca_file else None
            ),
        ) as client:
            try:
                oidc = OIDC(config, client)
                await oidc.discover()
                app.state.identity = IdentityService(config, app.state.identity_db, oidc)
                app.state.tenancy = TenancyService(config, app.state.db, app.state.identity_db)
                app.state.job_progress = JobProgressService(app.state.db)
                app.state.knowledge = KnowledgeService(app.state.db, app.state.tenancy.cursors)
                app.state.models = ModelService(app.state.db, config, app.state.tenancy.cursors)
                app.state.chat = ChatService(app.state.db, app.state.tenancy.cursors, config)
                if app.state.metrics_listener:
                    await app.state.metrics_listener.start(
                        app.state.telemetry_settings.metrics_host,
                        app.state.telemetry_settings.metrics_port,
                    )
                yield
            finally:
                if store:
                    await asyncio.to_thread(store.close)
                await app.state.db.dispose()
                await app.state.identity_db.dispose()
                if app.state.metrics_listener:
                    await app.state.metrics_listener.close()
                await asyncio.to_thread(app.state.telemetry.close)

    app = FastAPI(
        title="EvidenceDesk", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.state.settings = config

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = uuid4()
        try:
            response = await call_next(request)
        except Exception:
            response = problem_response(
                request, Problem(500, "internal_error", "The request could not be completed.")
            )
        response.headers["X-Request-ID"] = str(request.state.request_id)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return response

    def problem_response(request: Request, exc: Problem) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            media_type="application/problem+json",
            content={
                "type": f"https://evidencedesk.invalid/problems/{exc.code}",
                "title": exc.code,
                "status": exc.status,
                "code": exc.code,
                "detail": exc.detail,
                "request_id": str(request.state.request_id),
                "retryable": exc.retryable,
            },
        )

    @app.exception_handler(Problem)
    async def domain_error(request: Request, exc: Problem) -> JSONResponse:
        return problem_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            request, Problem(422, "validation_error", "The request contains invalid fields.")
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return problem_response(
            request, Problem(exc.status_code, "http_error", "The request is unavailable.")
        )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        try:
            async with asyncio.timeout(2):
                async with app.state.db.connect() as conn:
                    await conn.execute(text("SELECT 1"))
        except Exception as exc:
            raise Problem(503, "not_ready", "A required dependency is unavailable.", True) from exc
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(workspaces.router)
    app.include_router(chat.router)
    app.include_router(models.router)
    app.include_router(knowledge.router)
    app.include_router(documents.router)
    app.include_router(chunks.router)
    app.include_router(jobs.router)
    app.include_router(reports.router)
    app.include_router(briefs.router)
    app.include_router(evaluations.router)
    app.include_router(health.router)
    app.state.telemetry, app.state.metrics_listener, app.state.telemetry_settings = configured(
        "api", frozenset(app.openapi()["paths"]) | frozenset({"/openapi.json"})
    )
    app.add_middleware(HTTPMetricsMiddleware, telemetry=app.state.telemetry)
    return app

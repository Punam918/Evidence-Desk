"""Shared admission/readiness rules for API and worker, without browser credentials."""

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from evidencedesk.db import model_repository
from evidencedesk.db.session import AuthContext
from evidencedesk.db.tenancy_repository import policy
from evidencedesk.errors import Problem
from evidencedesk.models.registry import ModelAvailability, ModelConfig, load_registry
from sqlalchemy.ext.asyncio import AsyncConnection


class ModelSettings(Protocol):
    @property
    def model_config_path(self) -> Path: ...
    @property
    def openai_key_file(self) -> Path | None: ...
    @property
    def anthropic_key_file(self) -> Path | None: ...
    @property
    def google_key_file(self) -> Path | None: ...
    @property
    def local_model_url(self) -> str | None: ...


def endpoint_sha256(model: ModelConfig, settings: ModelSettings) -> str:
    endpoint = (
        "https://api.openai.com/v1"
        if model.adapter == "openai"
        else settings.local_model_url
        if model.adapter == "ollama"
        else "https://api.anthropic.com"
        if model.adapter == "anthropic"
        else "https://generativelanguage.googleapis.com/v1beta"
        if model.adapter == "google"
        else "unsupported"
    )
    return hashlib.sha256((endpoint or "").rstrip("/").encode()).hexdigest()


class ModelCatalog:
    def __init__(self, settings: ModelSettings):
        self.settings = settings
        self.registry = load_registry(settings.model_config_path)

    async def availability(
        self, conn: AsyncConnection, ctx: AuthContext
    ) -> list[ModelAvailability]:
        current = await policy(conn, ctx.workspace_id)
        health = await model_repository.health(conn)
        choices = []
        for model in self.registry:
            row = health.get(model.key, {})
            report = row.get("capability_report", {})
            reason = None
            if not model.enabled_after_conformance:
                reason = "Not enabled by the operator"
            elif current.privacy_mode == "local_only" and model.privacy_class == "hosted":
                reason = "Workspace permits private inference only"
            elif model.price_valid_until and model.price_valid_until < datetime.now(UTC).date():
                reason = "Pricing needs an operator refresh"
            elif model.requires_immutable_artifact and (
                not re.fullmatch(r"[a-f0-9]{64}", model.resolved_digest or "")
                or model.model_id.endswith(":latest")
            ):
                reason = "Local model artifact is not pinned"
            elif model.adapter == "openai" and (
                self.settings.openai_key_file is None or not self.settings.openai_key_file.is_file()
            ):
                reason = "Provider credentials are not configured"
            elif model.adapter == "anthropic" and (
                self.settings.anthropic_key_file is None
                or not self.settings.anthropic_key_file.is_file()
            ):
                reason = "Provider credentials are not configured"
            elif model.adapter == "google" and (
                self.settings.google_key_file is None or not self.settings.google_key_file.is_file()
            ):
                reason = "Provider credentials are not configured"
            elif model.adapter == "ollama" and not self.settings.local_model_url:
                reason = "Private inference endpoint is not configured"
            elif (
                row.get("state") != "closed"
                or not row.get("fresh")
                or report.get("config_sha256") != model.fingerprint
                or report.get("endpoint_sha256") != endpoint_sha256(model, self.settings)
                or report.get("passed") is not True
            ):
                reason = "Awaiting a current readiness probe"
            choices.append(
                ModelAvailability(
                    key=model.key,
                    display_name=model.model_id,
                    provider=model.adapter,
                    privacy_class=model.privacy_class,
                    available=reason is None,
                    supports_schema=reason is None,
                    context_cap=model.application_context_cap,
                    unavailable_reason=reason,
                )
            )
        return choices

    async def admit(self, conn: AsyncConnection, ctx: AuthContext, key: str) -> None:
        choices = await self.availability(conn, ctx)
        if not any(choice.available and (key == "auto" or choice.key == key) for choice in choices):
            raise Problem(
                409,
                "model_unavailable",
                "No ready model is eligible for this selection and workspace policy.",
            )

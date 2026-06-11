"""Operator model registry, never populated from a request or model output."""

import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from evidencedesk.identity.crypto import canonical_json
from evidencedesk.providers.base import Prices
from pydantic import BaseModel, ConfigDict, Field


class Rates(BaseModel):
    input: Decimal = Field(ge=0, allow_inf_nan=False)
    output: Decimal = Field(ge=0, allow_inf_nan=False)
    cached_input: Decimal = Field(ge=0, allow_inf_nan=False)


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,79}$")
    adapter: Literal["openai", "ollama", "anthropic", "google"]
    model_id: str
    enabled_after_conformance: bool
    privacy_class: Literal["hosted", "private"]
    application_context_cap: int = Field(ge=2048, le=16384)
    max_output_tokens: int = Field(ge=1, le=1024)
    requires_immutable_artifact: bool
    resolved_digest: str | None
    prices_usd_per_million: Rates
    price_date: date
    price_valid_until: date | None = None

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json")).encode()).hexdigest()

    @property
    def prices(self) -> Prices:
        rates = self.prices_usd_per_million
        return Prices(self.fingerprint, rates.input, rates.output, rates.cached_input)


def load_registry(path: Path) -> list[ModelConfig]:
    models = [ModelConfig.model_validate(row) for row in yaml.safe_load(path.read_text())["models"]]
    if len(models) > 100 or len({model.key for model in models}) != len(models):
        raise ValueError("Model keys must be unique and bounded")
    return sorted(models, key=lambda model: model.key)


class ModelAvailability(BaseModel):
    key: str
    display_name: str
    provider: str
    privacy_class: Literal["hosted", "private"]
    available: bool
    supports_schema: bool
    context_cap: int
    unavailable_reason: str | None

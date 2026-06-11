"""Operator-only private telemetry destinations; disabled until explicitly configured."""

import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TelemetrySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ED_", extra="ignore")
    metrics_host: str = "127.0.0.1"
    metrics_port: int | None = Field(default=None, ge=0, le=65535)
    metrics_token_file: Path | None = Field(default=None, repr=False)
    otlp_endpoint: str | None = None

    @field_validator("metrics_host")
    @classmethod
    def bind_address(cls, value: str) -> str:
        address = ipaddress.ip_address(value)
        if not (address.is_loopback or value == "0.0.0.0"):
            raise ValueError("Metrics bind requires loopback or container-only 0.0.0.0")
        return value

    @field_validator("otlp_endpoint")
    @classmethod
    def private_collector(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path != "/v1/traces"
        ):
            raise ValueError("Require a private OTLP HTTP /v1/traces endpoint without credentials")
        host = parsed.hostname
        if host not in ("127.0.0.1", "::1", "collector", "otel-collector"):
            raise ValueError("Collector must use loopback or the private Compose collector service")
        _ = parsed.port
        return value

    @model_validator(mode="after")
    def authenticated_listener(self) -> TelemetrySettings:
        if self.metrics_port is not None and self.metrics_token_file is None:
            raise ValueError("Metrics listener requires a mounted bearer token")
        return self

    def token(self) -> bytes:
        if self.metrics_token_file is None:
            raise ValueError("No metrics credential configured")
        value = self.metrics_token_file.read_bytes().strip()
        if (
            len(value) < 32
            or len(value) > 256
            or not value.isascii()
            or any(c < 33 or c > 126 for c in value)
            or b"REPLACE_ME" in value
        ):
            raise ValueError("Metrics token must contain 32-256 printable ASCII bytes")
        return value


def reject_automatic_content_tracing() -> None:
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        if os.getenv(name, "").lower() not in ("", "false", "0"):
            raise ValueError(
                "Automatic content tracing is not part of the reviewed telemetry pipeline"
            )

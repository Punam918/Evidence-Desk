"""Validated process configuration; secrets are mounted files, never loggable fields."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ED_", extra="ignore")
    env: Literal["development", "test", "production"] = "development"
    public_origin: str = "https://evidencedesk.local:8443"
    database_url: str = Field(repr=False)
    identity_database_url: str = Field(repr=False)
    dispatch_database_url: str = Field(default="", repr=False)
    checkpoint_database_url: str = Field(default="", repr=False)
    oidc_issuer: str
    oidc_client_id: str = "evidencedesk"
    oidc_client_secret_file: Path
    session_key_file: Path
    cursor_key_file: Path
    policy_config_path: Path = Path("contracts/config/defaults.yaml")
    model_config_path: Path = Path("contracts/config/models.yaml")
    model_artifact_lock: Path = Path("contracts/config/model-artifacts.lock.json")
    model_cache_path: Path = Path(".cache/models")
    tls_ca_file: Path | None = None
    openai_key_file: Path | None = Field(default=None, repr=False)
    anthropic_key_file: Path | None = Field(default=None, repr=False)
    google_key_file: Path | None = Field(default=None, repr=False)
    local_model_url: str | None = None
    local_model_key_file: Path | None = Field(default=None, repr=False)
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str | None = None
    s3_access_key_file: Path | None = Field(default=None, repr=False)
    s3_secret_key_file: Path | None = Field(default=None, repr=False)
    rewrite_enabled: bool = False
    daily_budget_ceiling_micro_usd: int = 2_000_000
    monthly_budget_ceiling_micro_usd: int = 20_000_000

    @field_validator("public_origin", "oidc_issuer")
    @classmethod
    def secure_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Require an operator-configured HTTPS URL without credentials/query/fragment"
            )
        return value.rstrip("/")

    @model_validator(mode="after")
    def origin_and_keys(self) -> Settings:
        if urlsplit(self.public_origin).path:
            raise ValueError("Public origin cannot contain a path")
        for path in [self.session_key_file, self.cursor_key_file]:
            secret = path.read_bytes().strip()
            if len(secret) < 32 or b"REPLACE_ME" in secret:
                raise ValueError("Session and cursor secrets require at least 32 random bytes")
        storage = [
            self.s3_endpoint,
            self.s3_bucket,
            self.s3_access_key_file,
            self.s3_secret_key_file,
        ]
        if any(storage) and not all(storage):
            raise ValueError("Object storage requires endpoint, bucket and both credential files")
        return self

    def session_key(self) -> bytes:
        return self.session_key_file.read_bytes().strip()

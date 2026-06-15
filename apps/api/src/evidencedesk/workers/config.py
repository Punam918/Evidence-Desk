"""Worker settings deliberately omit OIDC, browser sessions and paid provider credentials."""

from pathlib import Path

from evidencedesk.storage.s3 import S3Store
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StorageWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ED_", extra="ignore")
    database_url: str = Field(repr=False)
    dispatch_database_url: str = Field(repr=False)
    s3_endpoint: str
    s3_region: str = "us-east-1"
    s3_bucket: str
    s3_access_key_file: Path = Field(repr=False)
    s3_secret_key_file: Path = Field(repr=False)
    index_config_path: Path = Path("contracts/config/qdrant.yaml")

    def storage(self) -> S3Store:
        return S3Store(
            self.s3_endpoint,
            self.s3_region,
            self.s3_bucket,
            secret(self.s3_access_key_file),
            secret(self.s3_secret_key_file),
        )


class IngestionSettings(StorageWorkerSettings):
    model_artifact_lock: Path = Path("contracts/config/model-artifacts.lock.json")
    model_cache_path: Path = Path(".cache/models")
    parser_image_lock: Path = Path("infra/parser/image.lock.json")
    parser_socket_path: Path | None = None
    parser_service_lock: Path = Path("infra/parser/service.lock.json")


class IndexSettings(StorageWorkerSettings):
    qdrant_url: str
    qdrant_write_key_file: Path = Field(repr=False)


def secret(path: Path) -> str:
    value = path.read_text().strip()
    if not value or "REPLACE_ME" in value:
        raise ValueError("A mounted worker credential is empty or a placeholder")
    return value


class ChatSettings(BaseSettings):
    """Serving worker receives read-only index access and no object-store/browser credentials."""

    model_config = SettingsConfigDict(env_prefix="ED_", extra="ignore")
    database_url: str = Field(repr=False)
    identity_database_url: str = Field(repr=False)
    dispatch_database_url: str = Field(repr=False)
    checkpoint_database_url: str = Field(repr=False)
    qdrant_url: str
    qdrant_read_key_file: Path = Field(repr=False)
    index_config_path: Path = Path("contracts/config/qdrant.yaml")
    policy_config_path: Path = Path("contracts/config/defaults.yaml")
    model_config_path: Path = Path("contracts/config/models.yaml")
    model_artifact_lock: Path = Path("contracts/config/model-artifacts.lock.json")
    model_cache_path: Path = Path(".cache/models")
    local_tokenizer_lock: Path = Path("contracts/config/local-tokenizer.lock.json")
    openai_key_file: Path | None = Field(default=None, repr=False)
    anthropic_key_file: Path | None = Field(default=None, repr=False)
    google_key_file: Path | None = Field(default=None, repr=False)
    local_model_url: str | None = None
    local_model_key_file: Path | None = Field(default=None, repr=False)


class ProbeSettings(BaseSettings):
    """Operator probe has shared health access; no tenant, object, identity or index credentials."""

    model_config = SettingsConfigDict(env_prefix="ED_", extra="ignore")
    dispatch_database_url: str = Field(repr=False)
    database_url: str = Field(repr=False)  # Global local-inference advisory slot only.
    model_config_path: Path = Path("contracts/config/models.yaml")
    model_cache_path: Path = Path(".cache/models")
    local_tokenizer_lock: Path = Path("contracts/config/local-tokenizer.lock.json")
    openai_key_file: Path | None = Field(default=None, repr=False)
    anthropic_key_file: Path | None = Field(default=None, repr=False)
    google_key_file: Path | None = Field(default=None, repr=False)
    local_model_url: str | None = None
    local_model_key_file: Path | None = Field(default=None, repr=False)


class BriefSettings(BaseSettings):
    """Internal approved DB effects only; no model, identity, index or storage secrets."""

    model_config = SettingsConfigDict(env_prefix="ED_", extra="ignore")
    database_url: str = Field(repr=False)
    dispatch_database_url: str = Field(repr=False)
    checkpoint_database_url: str = Field(repr=False)

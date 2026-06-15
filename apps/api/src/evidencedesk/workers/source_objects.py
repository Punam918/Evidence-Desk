import json
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from evidencedesk.db.session import engine
from evidencedesk.lifecycle.source_objects import SourceObjectPurger
from evidencedesk.storage.s3 import S3Store
from evidencedesk.workers.config import secret
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ObjectPurgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ED_", extra="ignore")
    database_url: str = Field(repr=False)
    s3_endpoint: str
    s3_region: str = "us-east-1"
    s3_bucket: str
    s3_access_key_file: Path = Field(repr=False)
    s3_secret_key_file: Path = Field(repr=False)


async def purge(workspace: UUID, settings: ObjectPurgeSettings | None = None) -> int:
    settings = settings or ObjectPurgeSettings()  # type: ignore[call-arg]
    store = S3Store(
        settings.s3_endpoint,
        settings.s3_region,
        settings.s3_bucket,
        secret(settings.s3_access_key_file),
        secret(settings.s3_secret_key_file),
    )
    db = engine(settings.database_url, pool_size=1)
    try:
        page = await SourceObjectPurger(db, store).purge_workspace(workspace)
        print(json.dumps({"event": "source_objects", **asdict(page)}), flush=True)
        return 0
    finally:
        store.close()
        await db.dispose()

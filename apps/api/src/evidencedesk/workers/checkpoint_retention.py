import json
from dataclasses import asdict
from uuid import UUID

from evidencedesk.db.session import engine
from evidencedesk.lifecycle.checkpoints import CheckpointRetention
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RetentionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ED_", extra="ignore")
    database_url: str = Field(repr=False)
    checkpoint_database_url: str = Field(repr=False)


async def prune(
    workspace: UUID, after: UUID | None, settings: RetentionSettings | None = None
) -> int:
    settings = settings or RetentionSettings()  # type: ignore[call-arg]
    db = engine(settings.database_url, pool_size=1)
    try:
        page = await CheckpointRetention(db, settings.checkpoint_database_url).prune_workspace(
            workspace, after
        )
        print(
            json.dumps({"event": "checkpoint_retention", **asdict(page)}, default=str), flush=True
        )
        return 0
    finally:
        await db.dispose()

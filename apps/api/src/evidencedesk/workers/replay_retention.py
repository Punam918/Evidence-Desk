import json
from dataclasses import asdict
from uuid import UUID

from evidencedesk.db.session import engine
from evidencedesk.lifecycle.replay import ReplayRetention
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ReplayRetentionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ED_", extra="ignore")
    database_url: str = Field(repr=False)


async def prune(workspace: UUID, settings: ReplayRetentionSettings | None = None) -> int:
    settings = settings or ReplayRetentionSettings()  # type: ignore[call-arg]
    db = engine(settings.database_url, pool_size=1)
    try:
        page = await ReplayRetention(db).prune_workspace(workspace)
        print(json.dumps({"event": "replay_retention", **asdict(page)}), flush=True)
        return 0
    finally:
        await db.dispose()

import json
from dataclasses import asdict
from uuid import UUID

from evidencedesk.db.session import engine
from evidencedesk.lifecycle.conversation_retention import ConversationRetention
from evidencedesk.workers.replay_retention import ReplayRetentionSettings


async def expire(workspace: UUID, settings: ReplayRetentionSettings | None = None) -> int:
    settings = settings or ReplayRetentionSettings()  # type: ignore[call-arg]
    db = engine(settings.database_url, pool_size=1)
    try:
        page = await ConversationRetention(db).expire_workspace(workspace)
        print(json.dumps({"event": "conversation_expiry", **asdict(page)}), flush=True)
        return 0
    finally:
        await db.dispose()

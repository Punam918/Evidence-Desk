import json
from uuid import UUID

from evidencedesk.db.session import engine
from evidencedesk.lifecycle.redaction import SourceRedactor
from evidencedesk.workers.replay_retention import ReplayRetentionSettings


async def redact(workspace: UUID, settings: ReplayRetentionSettings | None = None) -> int:
    settings = settings or ReplayRetentionSettings()  # type: ignore[call-arg]
    db = engine(settings.database_url, pool_size=1)
    try:
        changed = await SourceRedactor(db).prune_workspace(workspace)
        print(
            json.dumps(
                {"event": "source_redaction", "processed": int(changed), "may_have_more": changed}
            ),
            flush=True,
        )
        return 0
    finally:
        await db.dispose()

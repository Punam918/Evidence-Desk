from uuid import uuid4

from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import engine
from evidencedesk.lifecycle.conversations import ConversationPurger
from evidencedesk.workers.config import BriefSettings
from evidencedesk.workers.runtime import consume


async def purge(once: bool, settings: BriefSettings | None = None) -> int:
    # Same least-privilege DB-only credential set as the brief worker.
    settings = settings or BriefSettings()  # type: ignore[call-arg]
    db, dispatch = engine(settings.database_url), engine(settings.dispatch_database_url)
    try:
        jobs = JobRepository(dispatch)
        purger = ConversationPurger(db, jobs, settings.checkpoint_database_url)
        worker = "conversation-purge-" + uuid4().hex
        return await consume(
            lambda: jobs.claim_conversation_purge(worker), purger.run, jobs.sweep, once=once
        )
    finally:
        await db.dispose()
        await dispatch.dispose()

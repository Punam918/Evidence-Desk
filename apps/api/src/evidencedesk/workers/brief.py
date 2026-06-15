from uuid import uuid4

from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import engine
from evidencedesk.workers.config import BriefSettings
from evidencedesk.workers.runtime import consume
from evidencedesk.workflows.brief_runner import BriefRunner


async def brief(once: bool, settings: BriefSettings | None = None) -> int:
    settings = settings or BriefSettings()  # type: ignore[call-arg]
    db, dispatch = engine(settings.database_url), engine(settings.dispatch_database_url)
    try:
        jobs = JobRepository(dispatch)
        runner = BriefRunner(db, jobs, settings.checkpoint_database_url)
        worker = "brief-" + uuid4().hex
        return await consume(
            lambda: jobs.claim("workflow", worker),
            lambda lease: runner.execute(lease, uuid4()),
            jobs.sweep,
            once=once,
        )
    finally:
        await db.dispose()
        await dispatch.dispose()

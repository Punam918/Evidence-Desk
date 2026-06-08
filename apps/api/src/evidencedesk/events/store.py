from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.chat.models import RunEvent
from evidencedesk.db import chat_repository, event_repository, usage_repository, workflow_repository
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext, authorize_connection, tenant_transaction
from evidencedesk.errors import Problem
from evidencedesk.events.models import Cancelled, Completed, Failed, Payload, Refused
from evidencedesk.jobs.models import Lease


class EventStore:
    def __init__(self, db: AsyncEngine):
        self.db = db

    async def append(self, ctx: AuthContext, lease: Lease, payload: Payload) -> RunEvent | None:
        if (
            lease.run_id is None
            or lease.workspace_id != ctx.workspace_id
            or lease.actor_id != ctx.user_id
        ):
            raise ValueError("Event scope must match the authoritative job lease")
        terminal = isinstance(payload, (Completed, Failed, Cancelled, Refused))
        async with tenant_transaction(self.db, ctx) as conn:
            if terminal:
                await chat_repository.lock_admission(conn)
            # Failure/cancel may redact an already revoked run; successful content always reauthorizes.
            if not isinstance(payload, (Failed, Cancelled)):
                await authorize_connection(conn, ctx, write=True)
            await JobRepository.fence(conn, lease)
            if isinstance(payload, Completed):
                cancelled = (
                    await conn.execute(
                        text(
                            "SELECT cancel_requested FROM app.runs WHERE workspace_id=:w AND id=:r AND user_id=:u FOR UPDATE"
                        ),
                        {"w": ctx.workspace_id, "r": lease.run_id, "u": ctx.user_id},
                    )
                ).scalar_one()
                if cancelled:
                    payload = Cancelled(reason="user")
            if terminal and not isinstance(payload, Completed):
                await usage_repository.settle_abandoned(conn, ctx, lease.run_id)
            if isinstance(payload, Completed):
                from evidencedesk.db import answer_repository

                run = await answer_repository.load(conn, ctx, lease)
                if run.manifest is not None:
                    await answer_repository.verify_manifest(conn, ctx, run, run.manifest)
                if (
                    payload.answer.status == "answered"
                    and not await usage_repository.has_generation(conn, ctx, lease.run_id)
                ):
                    raise Problem(
                        409,
                        "usage_missing",
                        "A generated answer needs a recorded provider attempt.",
                    )
                if not await usage_repository.all_settled(conn, ctx, lease.run_id):
                    raise Problem(
                        409, "usage_unsettled", "Usage must be settled before finalization."
                    )
                if not await chat_repository.run_sources_live(conn, ctx, lease.run_id):
                    raise Problem(409, "source_revoked", "A source was removed during generation.")
                refs = {
                    ref.evidence_id: ref
                    for ref in await chat_repository.evidence_references(conn, ctx, lease.run_id)
                }
                citation_ids = {citation.evidence_id for citation in payload.citations}
                if len(citation_ids) != len(payload.citations) or any(
                    not set(sentence.citation_ids) <= citation_ids
                    for sentence in payload.answer.sentences
                ):
                    raise Problem(
                        422, "invalid_citations", "The answer contains unbound citations."
                    )
                for citation in payload.citations:
                    ref = refs.get(citation.evidence_id)
                    if not ref or (
                        citation.chunk_id,
                        citation.document_id,
                        citation.revision_id,
                    ) != (ref.chunk_id, ref.document_id, ref.revision_id):
                        raise Problem(
                            422,
                            "invalid_citations",
                            "A citation was not part of this run's evidence.",
                        )
                    if not citation.available or not await chat_repository.source_live(
                        conn, ctx, citation.chunk_id, citation.document_id, citation.revision_id
                    ):
                        raise Problem(409, "source_revoked", "A source is no longer available.")
            event = await event_repository.append(conn, ctx, lease.run_id, payload)
            if terminal:
                await JobRepository.finish(conn, lease)
                await workflow_repository.complete_thread(
                    conn,
                    ctx,
                    lease,
                    "completed"
                    if event and event.type == "answer.completed"
                    else "cancelled"
                    if event and event.type == "run.cancelled"
                    else "failed",
                )
                await event_repository.reconcile_slots(conn)
            return event

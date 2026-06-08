from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evidencedesk.errors import LeaseLost
from evidencedesk.identity.crypto import canonical_json
from evidencedesk.jobs.models import JobKind, Lease


class JobRepository:
    def __init__(self, dispatch: AsyncEngine):
        self.dispatch = dispatch

    async def claim(self, kind: JobKind, worker: str) -> Lease | None:
        async with self.dispatch.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM app.claim_job(:kind,:worker)"),
                        {"kind": kind, "worker": worker},
                    )
                )
                .mappings()
                .first()
            )
            return Lease(**dict(row), worker_id=worker) if row else None

    async def renew(self, lease: Lease) -> None:
        async with self.dispatch.begin() as conn:
            renewed = (
                await conn.execute(
                    text("SELECT app.renew_job(:w,:id,:worker,:token)"), self.params(lease)
                )
            ).scalar_one()
            if not renewed:
                raise LeaseLost()

    async def claim_conversation_purge(self, worker: str) -> Lease | None:
        # A dedicated filtered claim leaves source/workspace purge jobs available
        # for their own handlers, rather than consuming unsupported work.
        async with self.dispatch.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM app.claim_conversation_purge(:worker)"),
                        {"worker": worker},
                    )
                )
                .mappings()
                .first()
            )
            return Lease(**dict(row), worker_id=worker) if row else None

    async def sweep(self) -> int:
        async with self.dispatch.begin() as conn:
            return int((await conn.execute(text("SELECT app.sweep_expired_jobs()"))).scalar_one())

    @staticmethod
    def params(lease: Lease) -> dict[str, Any]:
        return {
            "w": lease.workspace_id,
            "id": lease.job_id,
            "worker": lease.worker_id,
            "token": lease.lease_token,
        }

    @staticmethod
    async def fence(conn: AsyncConnection, lease: Lease) -> None:
        row = (
            await conn.execute(
                text("""SELECT 1 FROM app.jobs WHERE workspace_id=:w AND id=:id AND worker_id=:worker
            AND lease_token=:token AND status='running' AND lease_until>clock_timestamp() FOR UPDATE"""),
                JobRepository.params(lease),
            )
        ).first()
        if not row:
            raise LeaseLost()

    @staticmethod
    async def checkpoint(
        conn: AsyncConnection, lease: Lease, checkpoint: dict[str, Any], progress: int
    ) -> None:
        await JobRepository.fence(conn, lease)
        await conn.execute(
            text(
                "UPDATE app.jobs SET checkpoint=CAST(:checkpoint AS jsonb),progress=:progress,updated_at=clock_timestamp() WHERE workspace_id=:w AND id=:id"
            ),
            {
                "w": lease.workspace_id,
                "id": lease.job_id,
                "checkpoint": canonical_json(checkpoint),
                "progress": progress,
            },
        )

    @staticmethod
    async def finish(conn: AsyncConnection, lease: Lease) -> None:
        await JobRepository.fence(conn, lease)
        await conn.execute(
            text(
                "UPDATE app.jobs SET status='succeeded',progress=100,lease_until=NULL,updated_at=clock_timestamp() WHERE workspace_id=:w AND id=:id"
            ),
            {"w": lease.workspace_id, "id": lease.job_id},
        )

    @staticmethod
    async def defer_gate(conn: AsyncConnection, lease: Lease) -> None:
        await JobRepository.fence(conn, lease)
        await conn.execute(
            text(
                "UPDATE app.jobs SET status='retry_wait',attempts=greatest(0,attempts-1),available_at=clock_timestamp()+interval '1 second',lease_until=NULL WHERE workspace_id=:w AND id=:id"
            ),
            {"w": lease.workspace_id, "id": lease.job_id},
        )

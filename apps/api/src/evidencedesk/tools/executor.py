"""Deterministic paired read with durable deduplication and current authorization on replay."""

import asyncio
import hashlib

from evidencedesk.db import retrieval_repository, tool_repository
from evidencedesk.db.session import AuthContext, authorized_transaction
from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.jobs.models import Lease
from evidencedesk.retrieval.models import Profile, RetrievalResult
from evidencedesk.retrieval.service import Retriever
from evidencedesk.tools.registry import COMPARE, CompareInput
from evidencedesk.tools.snapshot import ComparisonSnapshot
from sqlalchemy.ext.asyncio import AsyncEngine


class ToolExecutor:
    def __init__(self, db: AsyncEngine, retriever: Retriever):
        self.db, self.retriever = db, retriever

    async def compare(
        self, ctx: AuthContext, lease: Lease, value: CompareInput, profile: Profile
    ) -> RetrievalResult:
        async with authorized_transaction(self.db, ctx, write=True) as conn:
            row = await tool_repository.begin(conn, ctx, lease, value)
            if row["status"] == "succeeded":
                result = await tool_repository.restore(conn, ctx, row, self.retriever.config)
                if (
                    result.config_sha256
                    != hashlib.sha256((self.retriever.hash + ":" + profile).encode()).hexdigest()
                    or result.profile != profile
                ):
                    raise Problem(
                        409, "workflow_incompatible", "Comparison retrieval settings changed."
                    )
                return result
        try:
            async with asyncio.timeout(COMPARE.timeout_seconds):
                result = await self.retriever.retrieve_revisions(
                    ctx, value.focus, [value.left_revision_id, value.right_revision_id], profile
                )
            async with authorized_transaction(self.db, ctx, write=True) as conn:
                await retrieval_repository.recheck(conn, ctx, result.scope, self.retriever.config)
                await tool_repository.finish(
                    conn, ctx, lease, row, ComparisonSnapshot.freeze(result)
                )
            return result
        except LeaseLost:
            raise
        except Problem, TimeoutError:
            async with authorized_transaction(self.db, ctx, write=True) as conn:
                await tool_repository.finish(conn, ctx, lease, row, None)
            raise

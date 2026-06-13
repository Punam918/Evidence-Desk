"""Reload evidence at each content boundary; an in-memory candidate is not authority."""

import hashlib
import unicodedata

from evidencedesk.db import retrieval_repository as repository
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.index.qdrant import IndexConfig
from evidencedesk.retrieval.models import Candidate, Evidence, RetrievalResult
from sqlalchemy.ext.asyncio import AsyncConnection


def check_query(query: str, result: RetrievalResult) -> str:
    normalized = unicodedata.normalize("NFC", query.strip())
    if (
        not 1 <= len(normalized) <= 2000
        or hashlib.sha256(normalized.encode()).hexdigest() != result.query_sha256
    ):
        raise Problem(422, "invalid_query", "Query does not match the retrieved evidence.")
    return normalized


async def reload_evidence(
    conn: AsyncConnection, ctx: AuthContext, result: RetrievalResult, config: IndexConfig
) -> tuple[Evidence, ...]:
    await repository.recheck(conn, ctx, result.scope, config)
    if len(result.evidence) > 20 or len({e.passage.chunk_id for e in result.evidence}) != len(
        result.evidence
    ):
        raise Problem(422, "invalid_evidence", "Invalid candidate identities.")
    refs = {r.revision_id: r for r in result.scope.revisions}
    candidates = []
    for rank, evidence in enumerate(result.evidence, 1):
        p = evidence.passage
        ref = refs.get(p.revision_id)
        if ref is None:
            raise Problem(404, "source_revoked", "Evidence is unavailable.")
        candidates.append(
            Candidate(
                p.chunk_id,
                "dense",
                rank,
                0.0,
                {
                    "workspace_id": str(ctx.workspace_id),
                    "kb_id": str(p.kb_id),
                    "document_id": str(p.document_id),
                    "revision_id": str(p.revision_id),
                    "profile_id": result.scope.profile_id,
                    "text_sha256": p.text_sha256,
                    "manifest_sha256": ref.manifest_sha256,
                },
            )
        )
    passages = await repository.hydrate(conn, ctx, result.scope, tuple(candidates))
    if any(passages.get(e.passage.chunk_id) != e.passage for e in result.evidence):
        raise Problem(404, "source_revoked", "Evidence changed or is unavailable.")
    return result.evidence

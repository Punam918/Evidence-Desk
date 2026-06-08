"""Fenced bounded read ledger under the run; graph checkpoints carry none of its content."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.ai.manifest import passage_hash
from evidencedesk.db import answer_repository, retrieval_repository
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.identity.crypto import canonical_json, digest
from evidencedesk.index.qdrant import IndexConfig
from evidencedesk.jobs.models import Lease
from evidencedesk.retrieval.models import Candidate, Evidence, RetrievalResult
from evidencedesk.tools.registry import COMPARE, CompareInput
from evidencedesk.tools.snapshot import ComparisonSnapshot


async def begin(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease, value: CompareInput
) -> dict[str, Any]:
    run = await answer_repository.load(conn, ctx, lease)
    if (
        run.intent != "compare"
        or value.focus != run.query
        or [str(value.left_revision_id), str(value.right_revision_id)]
        != run.metadata["revision_ids"]
    ):
        raise Problem(
            409, "tool_scope_mismatch", "Comparison arguments differ from the authorized run."
        )
    argument_hash = digest(canonical_json(value.model_dump(mode="json")))
    previous = run.metadata.get("comparison_read", {})
    if previous and previous.get("arguments_sha256") != argument_hash:
        raise Problem(
            409, "tool_scope_mismatch", "A comparison cannot change its arguments on replay."
        )
    if previous.get("status") == "failed":
        raise Problem(409, "tool_already_failed", "A failed comparison read cannot be repeated.")
    if previous.get("status") == "succeeded":
        return dict(previous)
    attempts = previous.get("attempts", 0) + 1
    if attempts > COMPARE.recovery_attempts:
        raise Problem(409, "tool_limit", "The comparison read budget is exhausted.")
    value_row = {
        "tool_name": COMPARE.name,
        "tool_version": COMPARE.version,
        "arguments_sha256": argument_hash,
        "status": "running",
        "attempts": attempts,
    }
    await answer_repository.metadata(conn, ctx, lease, {"comparison_read": value_row})
    return value_row


async def finish(
    conn: AsyncConnection,
    ctx: AuthContext,
    lease: Lease,
    row: dict[str, Any],
    snapshot: ComparisonSnapshot | None,
) -> None:
    await answer_repository.load(conn, ctx, lease)
    result = snapshot.model_dump(mode="json") if snapshot else None
    if result is not None and len(canonical_json(result).encode()) > 32768:
        raise Problem(
            422,
            "tool_result_too_large",
            "The paired evidence references exceed the tool result budget.",
        )
    await answer_repository.metadata(
        conn,
        ctx,
        lease,
        {
            "comparison_read": {
                **row,
                "status": "succeeded" if result is not None else "failed",
                "snapshot": result,
                "payload_sha256": digest(canonical_json(result)),
            }
        },
    )


async def restore(
    conn: AsyncConnection, ctx: AuthContext, row: dict[str, Any], config: IndexConfig
) -> RetrievalResult:
    if digest(canonical_json(row["snapshot"])) != row["payload_sha256"]:
        raise Problem(409, "tool_result_changed", "The saved comparison result changed.")
    saved = ComparisonSnapshot.model_validate(row["snapshot"])
    await retrieval_repository.recheck(conn, ctx, saved.scope, config)
    refs = {r.revision_id: r for r in saved.scope.revisions}
    candidates = tuple(
        Candidate(
            e.source.chunk_id,
            "dense",
            i,
            0,
            {
                "workspace_id": str(ctx.workspace_id),
                "kb_id": str(e.source.kb_id),
                "document_id": str(e.source.document_id),
                "revision_id": str(e.source.revision_id),
                "profile_id": saved.scope.profile_id,
                "text_sha256": e.source.text_sha256,
                "manifest_sha256": refs[e.source.revision_id].manifest_sha256,
            },
        )
        for i, e in enumerate(saved.evidence, 1)
    )
    passages = await retrieval_repository.hydrate(conn, ctx, saved.scope, candidates)
    evidence = []
    for item in saved.evidence:
        passage = passages.get(item.source.chunk_id)
        if passage is None or passage_hash(passage) != item.source.passage_sha256:
            raise Problem(
                409, "tool_result_changed", "A selected comparison passage is no longer available."
            )
        evidence.append(
            Evidence(
                passage,
                item.fused_score,
                item.dense_rank,
                item.dense_score,
                item.sparse_rank,
                item.sparse_score,
            )
        )
    return RetrievalResult(
        tuple(evidence),
        saved.scope,
        saved.query_sha256,
        saved.config_sha256,
        saved.profile,
        {},
        {},
        tuple(saved.degraded),
    )

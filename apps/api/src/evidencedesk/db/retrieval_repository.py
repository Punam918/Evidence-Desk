"""Resolve and recheck source authority under live tenant authorization, outside vector transport."""

import hashlib
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.db.knowledge_repository import get_kb
from evidencedesk.db.session import AuthContext
from evidencedesk.errors import Problem
from evidencedesk.index.qdrant import IndexConfig
from evidencedesk.ingestion.profile import IngestionProfile
from evidencedesk.retrieval.models import (
    Candidate,
    Passage,
    ResolvedScope,
    RevisionScope,
    ScopeChanged,
)

SOURCE = """SELECT r.id AS revision_id,r.document_id,r.kb_id,r.status,r.profile_hash,
    m.state,m.collection_name,m.profile_id,m.manifest_sha256,m.expected_points,m.verified_points,
    d.active_revision_id FROM app.document_revisions r
    JOIN app.documents d ON d.workspace_id=r.workspace_id AND d.id=r.document_id
    JOIN app.knowledge_bases k ON k.workspace_id=r.workspace_id AND k.id=r.kb_id
    LEFT JOIN app.index_manifests m ON m.workspace_id=r.workspace_id AND m.revision_id=r.id AND m.profile_id=r.embedding_profile_id
    WHERE r.workspace_id=:w AND d.deleted_at IS NULL AND k.deleted_at IS NULL"""


async def resolve(
    conn: AsyncConnection,
    ctx: AuthContext,
    kbs: tuple[UUID, ...],
    revisions: tuple[UUID, ...],
    config: IndexConfig,
) -> ResolvedScope:
    ids: tuple[UUID, ...]
    if revisions:
        if len(revisions) != 2 or len(set(revisions)) != 2:
            raise Problem(
                422, "invalid_scope", "Comparison requires exactly two different revisions."
            )
        condition = " AND r.id=ANY(CAST(:ids AS uuid[]))"
        ids = revisions
    else:
        if not 1 <= len(kbs) <= 10 or len(set(kbs)) != len(kbs):
            raise Problem(422, "invalid_scope", "Choose 1–10 different knowledge bases.")
        for kb in kbs:
            await get_kb(conn, ctx, kb)
        # Bound the active document set before joining manifests/revisions. With stale
        # planner statistics, the four-table join can otherwise scan thousands of
        # rows repeatedly before reaching LIMIT, even for a scope we must reject.
        active = (
            (
                await conn.execute(
                    text("""SELECT active_revision_id FROM app.documents
            WHERE workspace_id=:w AND kb_id=ANY(CAST(:kbs AS uuid[]))
            AND deleted_at IS NULL AND active_revision_id IS NOT NULL
            ORDER BY id LIMIT 4097"""),
                    {"w": ctx.workspace_id, "kbs": list(kbs)},
                )
            )
            .scalars()
            .all()
        )
        if len(active) > 4096:
            raise Problem(
                422,
                "scope_too_large",
                "This scope exceeds 4096 revisions. Choose fewer knowledge bases.",
            )
        condition = " AND r.id=d.active_revision_id AND r.id=ANY(CAST(:ids AS uuid[]))"
        ids = tuple(active)
    rows = (
        (
            await conn.execute(
                text(SOURCE + condition + " ORDER BY r.id LIMIT 4097"),
                {"w": ctx.workspace_id, "ids": list(ids)},
            )
        )
        .mappings()
        .all()
    )
    if revisions and (
        len(rows) != 2 or any(row["status"] not in ("published", "superseded") for row in rows)
    ):
        raise Problem(404, "not_found", "A comparison revision is unavailable.")
    if len(rows) > 4096:
        raise Problem(
            422,
            "scope_too_large",
            "This scope exceeds 4096 revisions. Choose fewer knowledge bases.",
        )
    profile = IngestionProfile()
    policy = (
        (
            await conn.execute(
                text(
                    "SELECT corpus_generation,version,embedding_profile FROM app.workspaces WHERE id=:w"
                ),
                {"w": ctx.workspace_id},
            )
        )
        .mappings()
        .one()
    )
    if policy["embedding_profile"] != profile.embedding_profile_id or any(
        row["state"] != "verified"
        or row["collection_name"] != config.collection
        or row["profile_id"] != profile.embedding_profile_id
        or row["profile_hash"] != profile.fingerprint
        or row["expected_points"] <= 0
        or row["verified_points"] != row["expected_points"]
        or (not revisions and row["status"] != "published")
        for row in rows
    ):
        raise Problem(
            503, "index_unavailable", "A selected source index is unavailable or rebuilding.", True
        )
    return ResolvedScope(
        ctx.workspace_id,
        ctx.user_id,
        tuple(sorted({row["kb_id"] for row in rows})) if revisions else kbs,
        tuple(
            RevisionScope(
                row["revision_id"],
                row["document_id"],
                row["kb_id"],
                row["manifest_sha256"],
                row["expected_points"],
            )
            for row in (
                sorted(rows, key=lambda r: revisions.index(r["revision_id"])) if revisions else rows
            )
        ),
        config.collection,
        profile.embedding_profile_id,
        policy["corpus_generation"],
        policy["version"],
        "compare" if revisions else "ask",
    )


async def recheck(
    conn: AsyncConnection, ctx: AuthContext, scope: ResolvedScope, config: IndexConfig
) -> None:
    if ctx.workspace_id != scope.workspace_id or ctx.user_id != scope.user_id:
        raise Problem(404, "not_found", "Search scope is unavailable.")
    current = await resolve(
        conn,
        ctx,
        scope.kb_ids,
        tuple(r.revision_id for r in scope.revisions) if scope.mode == "compare" else (),
        config,
    )
    if (
        current.revisions != scope.revisions
        or current.corpus_generation != scope.corpus_generation
        or current.policy_revision != scope.policy_revision
    ):
        raise ScopeChanged()


async def hydrate(
    conn: AsyncConnection, ctx: AuthContext, scope: ResolvedScope, candidates: tuple[Candidate, ...]
) -> dict[UUID, Passage]:
    if not candidates:
        return {}
    if (
        len(candidates) > 80
        or ctx.workspace_id != scope.workspace_id
        or ctx.user_id != scope.user_id
    ):
        raise Problem(422, "invalid_scope", "Candidate scope is invalid.")
    refs = {r.revision_id: r for r in scope.revisions}
    rows = (
        (
            await conn.execute(
                text("""SELECT c.*,d.title,r.revision_no,r.published_at FROM app.chunks c
        JOIN app.document_revisions r ON r.workspace_id=c.workspace_id AND r.id=c.revision_id
        JOIN app.documents d ON d.workspace_id=c.workspace_id AND d.id=c.document_id
        JOIN app.knowledge_bases k ON k.workspace_id=c.workspace_id AND k.id=c.kb_id
        WHERE c.workspace_id=:w AND c.id=ANY(CAST(:ids AS uuid[])) AND c.revision_id=ANY(CAST(:revisions AS uuid[]))
        AND c.kb_id=ANY(CAST(:kbs AS uuid[])) AND d.deleted_at IS NULL AND k.deleted_at IS NULL"""),
                {
                    "w": ctx.workspace_id,
                    "ids": list({c.id for c in candidates}),
                    "revisions": list(refs),
                    "kbs": list(scope.kb_ids),
                },
            )
        )
        .mappings()
        .all()
    )
    expected = {row["id"]: row for row in rows}
    valid = {}
    for candidate in candidates:
        row = expected.get(candidate.id)
        if row is None:
            continue
        ref = refs[row["revision_id"]]
        payload = {
            "workspace_id": str(ctx.workspace_id),
            "kb_id": str(row["kb_id"]),
            "document_id": str(row["document_id"]),
            "revision_id": str(row["revision_id"]),
            "profile_id": scope.profile_id,
            "text_sha256": row["text_sha256"],
            "manifest_sha256": ref.manifest_sha256,
        }
        if (
            candidate.payload != payload
            or row["document_id"] != ref.document_id
            or row["kb_id"] != ref.kb_id
        ):
            continue
        if hashlib.sha256(row["text"].encode()).hexdigest() != row["text_sha256"]:
            raise Problem(
                503,
                "source_integrity_failure",
                "Authoritative source text failed integrity checking.",
                True,
            )
        valid[candidate.id] = Passage(
            row["id"],
            row["document_id"],
            row["revision_id"],
            row["kb_id"],
            row["title"],
            row["text"],
            row["text_sha256"],
            row["token_count"],
            row["ordinal"],
            row["page_start"],
            row["page_end"],
            row["char_start"],
            row["char_end"],
            tuple(row["heading_path"]),
            row["source_locator"],
            row["revision_no"],
            row["published_at"],
        )
    return valid

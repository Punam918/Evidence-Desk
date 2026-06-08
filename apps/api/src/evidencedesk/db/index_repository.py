"""PostgreSQL authority for immutable index manifests and at-least-once delivery."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evidencedesk.db.session import AuthContext, authorize_connection
from evidencedesk.embeddings.models import VectorArtifact
from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.index.models import Delivery
from evidencedesk.index.qdrant import IndexConfig


class OutboxRepository:
    def __init__(self, dispatch: AsyncEngine):
        self.dispatch = dispatch

    async def claim(self) -> Delivery | None:
        async with self.dispatch.begin() as conn:
            row = (
                (await conn.execute(text("SELECT * FROM app.claim_index_outbox()")))
                .mappings()
                .first()
            )
            return Delivery(**dict(row)) if row else None

    async def renew(self, lease: Delivery) -> None:
        async with self.dispatch.begin() as conn:
            if not (
                await conn.execute(
                    text("SELECT app.renew_index_outbox(:w,:id,:token)"), params(lease)
                )
            ).scalar_one():
                raise LeaseLost()

    async def known_chunks(self, ids: list[UUID]) -> set[UUID]:
        if len(ids) > 100:
            raise ValueError("Chunk ownership probe is bounded to100 IDs")
        async with self.dispatch.begin() as conn:
            return set(
                (
                    await conn.execute(
                        text("SELECT app.known_chunk_ids(CAST(:ids AS uuid[]))"), {"ids": ids}
                    )
                ).scalars()
            )


def params(lease: Delivery) -> dict[str, Any]:
    return {
        "w": lease.workspace_id,
        "id": lease.outbox_id,
        "r": lease.revision_id,
        "p": lease.profile_id,
        "op": lease.operation,
        "token": lease.lease_token,
    }


async def fence(conn: AsyncConnection, lease: Delivery) -> None:
    if not (
        await conn.execute(
            text("""SELECT 1 FROM app.index_outbox WHERE workspace_id=:w
        AND id=:id AND revision_id=:r AND profile_id=:p AND operation=:op AND lease_token=:token
        AND state='leased' AND lease_until>clock_timestamp() FOR UPDATE"""),
            params(lease),
        )
    ).first():
        raise LeaseLost()


async def manifest(
    conn: AsyncConnection, workspace: UUID, revision: UUID, profile: str
) -> dict[str, Any]:
    row = (
        (
            await conn.execute(
                text("""SELECT m.*,r.document_id,r.kb_id,r.status AS revision_status,
        r.profile_hash,r.chunk_count,r.manifest_key,r.object_key,r.content_sha256,r.byte_size,d.version AS document_version,d.latest_revision_id,
        d.active_revision_id,w.embedding_profile,
        (w.deleted_at IS NOT NULL OR d.deleted_at IS NOT NULL OR k.deleted_at IS NOT NULL
         OR r.status IN ('deleting','deleted')) AS tombstoned
        FROM app.index_manifests m JOIN app.document_revisions r ON r.workspace_id=m.workspace_id AND r.id=m.revision_id
        JOIN app.documents d ON d.workspace_id=r.workspace_id AND d.id=r.document_id
        JOIN app.knowledge_bases k ON k.workspace_id=r.workspace_id AND k.id=r.kb_id
        JOIN app.workspaces w ON w.id=r.workspace_id
        WHERE m.workspace_id=:w AND m.revision_id=:r AND m.profile_id=:p"""),
                {"w": workspace, "r": revision, "p": profile},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise Problem(404, "index_manifest_missing", "Index manifest is unavailable.")
    return dict(row)


async def check_chunks(conn: AsyncConnection, artifact: VectorArtifact) -> None:
    rows = (
        await conn.execute(
            text("""SELECT id,ordinal,text_sha256 FROM app.chunks
        WHERE workspace_id=:w AND revision_id=:r AND kb_id=:kb AND document_id=:d
        AND text_sha256=encode(sha256(convert_to(text,'UTF8')),'hex') ORDER BY ordinal"""),
            {
                "w": artifact.workspace_id,
                "r": artifact.revision_id,
                "kb": artifact.kb_id,
                "d": artifact.document_id,
            },
        )
    ).all()
    if [tuple(r) for r in rows] != [(p.id, p.ordinal, p.text_sha256) for p in artifact.points]:
        raise Problem(
            422, "chunk_integrity_failure", "Index identities differ from authoritative chunks."
        )


async def stage(
    conn: AsyncConnection, artifact: VectorArtifact, key: str, sha: str, config: IndexConfig
) -> None:
    await check_chunks(conn, artifact)
    args = {
        "w": artifact.workspace_id,
        "r": artifact.revision_id,
        "p": artifact.profile_id,
        "collection": config.collection,
        "n": len(artifact.points),
        "sha": sha,
        "key": key,
    }
    await conn.execute(
        text("""INSERT INTO app.index_manifests(workspace_id,revision_id,profile_id,
        collection_name,state,expected_points,manifest_sha256,artifact_key)
        VALUES(:w,:r,:p,:collection,'staged',:n,:sha,:key) ON CONFLICT DO NOTHING"""),
        args,
    )
    row = await manifest(conn, artifact.workspace_id, artifact.revision_id, artifact.profile_id)
    if (
        row["collection_name"],
        row["expected_points"],
        row["manifest_sha256"],
        row["artifact_key"],
    ) != (config.collection, len(artifact.points), sha, key):
        raise Problem(422, "index_manifest_conflict", "An immutable index manifest has changed.")
    await conn.execute(
        text("""INSERT INTO app.index_outbox(workspace_id,id,revision_id,profile_id,operation,operation_key)
        VALUES(:w,:id,:r,:p,'upsert',:opkey) ON CONFLICT(workspace_id,operation_key) DO NOTHING"""),
        {
            **args,
            "id": uuid4(),
            "opkey": f"upsert:{artifact.revision_id}:{artifact.profile_id}:{sha}",
        },
    )


async def delivery_state(
    conn: AsyncConnection, lease: Delivery, config: IndexConfig
) -> dict[str, Any]:
    # Workspace first: same lock order as publication, deletion and role changes. No network in this transaction.
    await conn.execute(text("SELECT id FROM app.workspaces WHERE id=:w FOR SHARE"), params(lease))
    await fence(conn, lease)
    row = await manifest(conn, lease.workspace_id, lease.revision_id, lease.profile_id)
    if row["collection_name"] != config.collection:
        raise Problem(409, "index_config_mismatch", "Delivery belongs to another index generation.")
    if lease.operation == "purge":
        if not row["tombstoned"]:
            raise Problem(409, "purge_not_authorized", "Live source points cannot be purged.")
    else:
        if row["tombstoned"] or row["state"] in ("purging", "purged"):
            raise Problem(409, "source_deleted", "The source was removed.")
        if row["embedding_profile"] != lease.profile_id or row["revision_status"] in (
            "failed",
            "cancelled",
        ):
            raise Problem(409, "revision_unavailable", "The revision cannot be indexed.")
        if row["revision_status"] not in ("published", "superseded"):
            actor = (
                await conn.execute(
                    text("""SELECT requested_by FROM app.jobs WHERE workspace_id=:w
                AND revision_id=:r AND kind='ingest' AND status IN ('queued','running','retry_wait')"""),
                    params(lease),
                )
            ).scalar_one_or_none()
            if actor is None:
                raise Problem(409, "revision_unavailable", "The ingestion job is no longer active.")
            await authorize_connection(
                conn, AuthContext(actor, lease.workspace_id, "editor", 0, uuid4()), "editor"
            )
        # Published immutable indexes are repaired by the internal queue even if the original uploader left.
        # This grants no browser/content authority; retrieval separately reauthorizes every user.
    return row


async def mark_verified(conn: AsyncConnection, lease: Delivery, config: IndexConfig) -> None:
    await delivery_state(conn, lease, config)
    await conn.execute(
        text("""UPDATE app.index_manifests SET state='verified',verified_points=expected_points,
        verified_at=clock_timestamp(),updated_at=clock_timestamp() WHERE workspace_id=:w AND revision_id=:r AND profile_id=:p"""),
        params(lease),
    )
    await complete(conn, lease)
    await conn.execute(
        text("""UPDATE app.jobs SET available_at=clock_timestamp()
        WHERE workspace_id=:w AND revision_id=:r AND kind='ingest' AND status='retry_wait'"""),
        params(lease),
    )


async def complete(conn: AsyncConnection, lease: Delivery) -> None:
    await fence(conn, lease)
    await conn.execute(
        text(
            "UPDATE app.index_outbox SET state='done',lease_until=NULL,error_code=NULL WHERE workspace_id=:w AND id=:id"
        ),
        params(lease),
    )


async def fail(conn: AsyncConnection, lease: Delivery, problem: Problem) -> None:
    await fence(conn, lease)
    await conn.execute(
        text("""UPDATE app.index_outbox SET state=CASE WHEN :retry AND attempts<3 THEN 'pending' ELSE 'dead' END,
        error_code=:code,lease_until=NULL,next_attempt_at=clock_timestamp()+
        ((CASE attempts WHEN 1 THEN 2 WHEN 2 THEN 8 ELSE 30 END)*(0.8+random()*0.4))*interval '1 second'
        WHERE workspace_id=:w AND id=:id"""),
        {**params(lease), "retry": problem.retryable, "code": problem.code},
    )
    # Never reset a deletion state on a failed/delayed upsert.
    if lease.operation == "upsert":
        await conn.execute(
            text("""UPDATE app.index_manifests SET state=CASE
            WHEN EXISTS(SELECT 1 FROM app.index_outbox WHERE workspace_id=:w AND id=:id AND state='dead')
            THEN 'failed' ELSE 'unavailable' END,verified_points=0,verified_at=NULL,updated_at=clock_timestamp()
            WHERE workspace_id=:w AND revision_id=:r AND profile_id=:p AND state NOT IN ('purging','purged')"""),
            params(lease),
        )


async def enqueue_purge(
    conn: AsyncConnection, workspace: UUID, kb: UUID | None = None, revision: UUID | None = None
) -> None:
    """Called in the parent tombstone transaction; safe to repeat during reconciliation."""
    args = {"w": workspace, "kb": kb, "revision": revision}
    clause = " AND r.kb_id=:kb" if kb else ""
    if revision:
        clause += " AND r.id=:revision"
    revisions = (
        (
            await conn.execute(
                text(
                    """SELECT m.revision_id,m.profile_id,m.manifest_sha256 FROM app.index_manifests m
        JOIN app.document_revisions r ON r.workspace_id=m.workspace_id AND r.id=m.revision_id
        WHERE m.workspace_id=:w"""
                    + clause
                    + " ORDER BY m.revision_id,m.profile_id"
                ),
                args,
            )
        )
        .mappings()
        .all()
    )
    for row in revisions:
        values = {**args, "r": row["revision_id"], "p": row["profile_id"]}
        await conn.execute(
            text("""UPDATE app.index_outbox SET state='dead',lease_token=lease_token+1,lease_until=NULL,
            error_code='source_deleted' WHERE workspace_id=:w AND revision_id=:r AND profile_id=:p
            AND operation='upsert' AND state IN ('pending','leased')"""),
            values,
        )
        await conn.execute(
            text("""UPDATE app.index_manifests SET state='purging',verified_points=0,verified_at=NULL,
            updated_at=clock_timestamp() WHERE workspace_id=:w AND revision_id=:r AND profile_id=:p AND state<>'purged'"""),
            values,
        )
        await conn.execute(
            text("""INSERT INTO app.index_outbox(workspace_id,id,revision_id,profile_id,operation,operation_key)
            VALUES(:w,:id,:r,:p,'purge',:key) ON CONFLICT(workspace_id,operation_key) DO NOTHING"""),
            {
                **values,
                "id": uuid4(),
                "key": f"purge:{row['revision_id']}:{row['profile_id']}:{row['manifest_sha256']}",
            },
        )

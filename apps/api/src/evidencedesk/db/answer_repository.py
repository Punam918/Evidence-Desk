"""Fenced business ledger for grounded execution. Never performs network/model work."""

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from evidencedesk.ai.manifest import PromptManifest, history_hash, passage_hash
from evidencedesk.ai.memory import HistoryPair
from evidencedesk.chat.models import Answer, EvidenceReference
from evidencedesk.db import event_repository, usage_repository
from evidencedesk.db.history_repository import selected_history
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext
from evidencedesk.db.tenancy_repository import policy
from evidencedesk.errors import Problem
from evidencedesk.events.models import Delta
from evidencedesk.identity.crypto import canonical_json
from evidencedesk.jobs.models import Lease
from evidencedesk.providers.base import ModelFault, ModelResult, Usage
from evidencedesk.retrieval.models import Passage


@dataclass(frozen=True)
class RunRecord:
    id: UUID
    conversation_id: UUID
    assistant_id: UUID
    query: str
    intent: Literal["ask", "compare"]
    requested_model_key: str
    config_sha256: str
    metadata: dict[str, Any]

    @property
    def manifest(self) -> PromptManifest | None:
        raw = self.metadata.get("prompt_manifest")
        try:
            return PromptManifest.model_validate(raw) if raw is not None else None
        except ValueError as exc:
            raise Problem(
                409, "invalid_execution_manifest", "Stored execution references are invalid."
            ) from exc


async def load(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> RunRecord:
    await JobRepository.fence(conn, lease)
    await usage_repository.check_run_egress(conn, ctx, lease)
    row = (
        (
            await conn.execute(
                text("""SELECT r.*,m.text AS query FROM app.runs r
        JOIN app.messages m ON m.workspace_id=r.workspace_id AND m.id=r.user_message_id
        WHERE r.workspace_id=:w AND r.id=:id AND r.user_id=:u FOR UPDATE OF r"""),
                {"w": ctx.workspace_id, "id": lease.run_id, "u": ctx.user_id},
            )
        )
        .mappings()
        .one()
    )
    return RunRecord(
        row["id"],
        row["conversation_id"],
        row["assistant_message_id"],
        unicodedata.normalize("NFC", row["query"].strip()),
        row["intent"],
        row["requested_model_key"],
        row["config_sha256"],
        dict(row["execution_metadata"]),
    )


async def metadata(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease, values: dict[str, Any]
) -> None:
    await JobRepository.fence(conn, lease)
    await conn.execute(
        text("""UPDATE app.runs SET execution_metadata=execution_metadata || CAST(:v AS jsonb)
        WHERE workspace_id=:w AND id=:id AND user_id=:u"""),
        {"w": ctx.workspace_id, "id": lease.run_id, "u": ctx.user_id, "v": canonical_json(values)},
    )


async def hydrate(
    conn: AsyncConnection, ctx: AuthContext, manifest: PromptManifest
) -> tuple[Passage, ...]:
    if not manifest.sources:
        return ()
    rows = (
        (
            await conn.execute(
                text("""SELECT c.*,d.title,r.revision_no,r.published_at FROM app.chunks c
        JOIN app.documents d ON d.workspace_id=c.workspace_id AND d.id=c.document_id
        JOIN app.document_revisions r ON r.workspace_id=c.workspace_id AND r.id=c.revision_id
        JOIN app.knowledge_bases k ON k.workspace_id=c.workspace_id AND k.id=c.kb_id
        WHERE c.workspace_id=:w AND c.id=ANY(CAST(:ids AS uuid[]))
        AND d.deleted_at IS NULL AND k.deleted_at IS NULL AND r.status IN ('published','superseded')"""),
                {"w": ctx.workspace_id, "ids": [s.chunk_id for s in manifest.sources]},
            )
        )
        .mappings()
        .all()
    )
    passages = {
        row["id"]: Passage(
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
        for row in rows
    }
    result = []
    for source in manifest.sources:
        p = passages.get(source.chunk_id)
        if (
            p is None
            or (p.document_id, p.revision_id, p.kb_id, p.text_sha256)
            != (source.document_id, source.revision_id, source.kb_id, source.text_sha256)
            or hashlib.sha256(p.text.encode()).hexdigest() != source.text_sha256
            or passage_hash(p) != source.passage_sha256
        ):
            raise Problem(409, "source_revoked", "A frozen source changed or is unavailable.")
        result.append(p)
    return tuple(result)


async def verify_manifest(
    conn: AsyncConnection, ctx: AuthContext, run: RunRecord, manifest: PromptManifest
) -> tuple[tuple[Passage, ...], tuple[HistoryPair, ...]]:
    current = await policy(conn, ctx.workspace_id)
    if current.version != manifest.policy_revision:
        raise Problem(409, "policy_changed", "Workspace policy changed during this run.")
    if (
        hashlib.sha256(run.query.encode()).hexdigest() != manifest.query_sha256
        or run.intent != manifest.mode
    ):
        raise Problem(409, "invalid_execution_manifest", "Stored query binding changed.")
    if manifest.kb_ids != [UUID(v) for v in run.metadata["kb_ids"]] or (
        run.intent == "compare"
        and manifest.revision_ids != [UUID(v) for v in run.metadata["revision_ids"]]
    ):
        raise Problem(409, "invalid_execution_manifest", "Stored source scope changed.")
    if any(
        s.kb_id not in manifest.kb_ids or s.revision_id not in manifest.revision_ids
        for s in manifest.sources
    ):
        raise Problem(409, "invalid_execution_manifest", "Source is outside the selected scope.")
    passages = await hydrate(conn, ctx, manifest)
    pairs = await selected_history(conn, ctx, run.id, manifest.history_ids)
    if history_hash(pairs) != manifest.history_sha256:
        raise Problem(409, "source_revoked", "Private history changed or is unavailable.")
    return passages, pairs


async def save_manifest(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease, manifest: PromptManifest
) -> None:
    run = await load(conn, ctx, lease)
    if run.manifest is not None:
        if run.manifest != manifest:
            raise Problem(409, "execution_conflict", "The frozen prompt cannot be replaced.")
        return
    if (
        await conn.execute(
            text(
                "SELECT 1 FROM app.model_usage WHERE workspace_id=:w AND run_id=:r AND purpose='generation' LIMIT 1"
            ),
            {"w": ctx.workspace_id, "r": lease.run_id},
        )
    ).first():
        raise Problem(409, "provider_interrupted", "Cannot change evidence after an attempt.")
    await verify_manifest(conn, ctx, run, manifest)
    await persist_manifest(conn, ctx, lease, manifest)


async def persist_manifest(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease, manifest: PromptManifest
) -> None:
    """Internal write after an initial freeze or checked one-way fallback transition."""
    await metadata(
        conn, ctx, lease, {"prompt_manifest": manifest.model_dump(mode="json"), "draft_cursor": 0}
    )
    refs = [
        EvidenceReference(
            evidence_id=f"E{i}",
            chunk_id=s.chunk_id,
            document_id=s.document_id,
            revision_id=s.revision_id,
        ).model_dump(mode="json")
        for i, s in enumerate(manifest.sources, 1)
    ]
    await conn.execute(
        text("""UPDATE app.runs SET evidence=CAST(:e AS jsonb),degraded=CAST(:d AS jsonb),
        model_key=:model,prompt_version=:prompt_version WHERE workspace_id=:w AND id=:r AND user_id=:u"""),
        {
            "w": ctx.workspace_id,
            "r": lease.run_id,
            "u": ctx.user_id,
            "e": canonical_json(refs),
            "d": canonical_json(manifest.degraded),
            "model": manifest.model_key,
            "prompt_version": manifest.prompt_version,
        },
    )


async def egress(conn: AsyncConnection, ctx: AuthContext, lease: Lease) -> None:
    run = await load(conn, ctx, lease)
    if run.manifest is None or run.manifest.abstention is not None:
        raise Problem(409, "invalid_execution_manifest", "A generation prompt is not frozen.")
    await verify_manifest(conn, ctx, run, run.manifest)


async def stored_result(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease
) -> tuple[UUID, ModelResult] | None:
    await JobRepository.fence(conn, lease)
    row = (
        (
            await conn.execute(
                text("""SELECT id,response,request_sha256 FROM app.model_usage
        WHERE workspace_id=:w AND run_id=:r AND user_id=:u AND purpose='generation'
        AND execution_state='response_stored' AND state IN ('settled','estimated') ORDER BY attempt_no DESC LIMIT 1"""),
                {"w": ctx.workspace_id, "r": lease.run_id, "u": ctx.user_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    run = await load(conn, ctx, lease)
    if run.manifest is None or row["request_sha256"] != run.manifest.provider_request_sha256:
        raise Problem(
            409, "invalid_execution_manifest", "Stored result does not match the frozen request."
        )
    raw = row["response"]
    try:
        return row["id"], ModelResult(
            raw["status"],
            Answer.model_validate(raw["answer"]) if raw["answer"] else None,
            Usage.model_validate(raw["usage"]) if raw["usage"] else None,
            raw["resolved_model_id"],
            raw["provider_response_id"],
            raw["raw_text"],
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise Problem(409, "invalid_model_output", "Stored provider result is invalid.") from exc


async def draft(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease, offset: int, payload: Delta
) -> None:
    run = await load(conn, ctx, lease)
    if run.manifest is None:
        raise Problem(409, "invalid_execution_manifest", "No frozen evidence exists.")
    await verify_manifest(conn, ctx, run, run.manifest)
    cursor = run.metadata.get("draft_cursor", 0)
    if cursor > offset:
        return
    if cursor != offset:
        raise Problem(409, "invalid_execution_manifest", "Draft progress is inconsistent.")
    await event_repository.append(conn, ctx, run.id, payload)
    await metadata(conn, ctx, lease, {"draft_cursor": offset + 1})


@dataclass(frozen=True)
class AttemptRecord:
    id: UUID
    model_key: str
    request_sha256: str
    price_version: str
    state: str
    execution: str
    fault: ModelFault | None


async def attempts(
    conn: AsyncConnection, ctx: AuthContext, lease: Lease
) -> tuple[AttemptRecord, ...]:
    await load(conn, ctx, lease)
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT * FROM app.model_usage WHERE workspace_id=:w AND run_id=:r AND user_id=:u AND purpose='generation' ORDER BY attempt_no"
                ),
                {"w": ctx.workspace_id, "r": lease.run_id, "u": ctx.user_id},
            )
        )
        .mappings()
        .all()
    )
    return tuple(
        AttemptRecord(
            row["id"],
            row["model_key"],
            row["request_sha256"],
            row["price_version"],
            row["state"],
            row["execution_state"],
            ModelFault(
                row["failure_code"] or "provider_interrupted",
                "definite_failure" if row["execution_state"] == "definite_failure" else "ambiguous",
                row["failure_retryable"],
                row["retry_after_seconds"],
            )
            if row["execution_state"] in ("definite_failure", "ambiguous", "issued")
            else None,
        )
        for row in rows
    )


async def switch_manifest(
    conn: AsyncConnection,
    ctx: AuthContext,
    lease: Lease,
    previous: PromptManifest,
    manifest: PromptManifest,
) -> None:
    run = await load(conn, ctx, lease)
    if run.manifest == manifest:
        return
    rows = await attempts(conn, ctx, lease)
    if (
        run.manifest != previous
        or run.requested_model_key != "auto"
        or run.metadata.get("prior_manifests")
        or run.metadata.get("draft_cursor", 0) != 0
        or not 1 <= len(rows) <= 2
        or any(
            row.execution != "definite_failure"
            or row.state != "settled"
            or row.model_key != previous.model_key
            or row.request_sha256 != previous.provider_request_sha256
            or row.price_version != previous.model_sha256
            for row in rows
        )
        or manifest.model_key == previous.model_key
    ):
        raise Problem(409, "execution_conflict", "Fallback cannot replace this execution.")
    if (
        manifest.query_sha256 != previous.query_sha256
        or manifest.revision_ids != previous.revision_ids
        or manifest.policy_revision != previous.policy_revision
        or any(source not in previous.sources for source in manifest.sources)
        or any(uid not in previous.history_ids for uid in manifest.history_ids)
    ):
        raise Problem(409, "execution_conflict", "Fallback changed its authorized source scope.")
    await verify_manifest(conn, ctx, run, previous)
    await verify_manifest(conn, ctx, run, manifest)
    await metadata(
        conn,
        ctx,
        lease,
        {
            "prior_manifests": [previous.model_dump(mode="json")],
            "selected_model": {"key": manifest.model_key, "sha256": manifest.model_sha256},
        },
    )
    await persist_manifest(conn, ctx, lease, manifest)

from uuid import uuid4

import psycopg
import pytest
from evidencedesk.db.session import AuthContext, engine
from evidencedesk.errors import Problem
from evidencedesk.tools.executor import ToolExecutor
from evidencedesk.tools.registry import CompareInput

from tests.integration.answers.test_execution import grounded as grounded
from tests.integration.retrieval.test_retrieval import publish_fixture

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "case",
    [
        "replay",
        "source_revoked",
        "policy_changed",
        "query_spoof",
        "revision_spoof",
        "failed_repeat",
        "call_cap",
        "payload_changed",
    ],
)
async def test_comparison_read_ledger_keeps_scope_and_bounds_replay(
    grounded, database, object_store, services, tmp_path, monkeypatch, case
):
    second, _ = await publish_fixture(
        grounded.client,
        grounded.base + f"/knowledge-bases/{grounded.kb}/documents",
        grounded.h,
        database,
        object_store,
        services,
        tmp_path,
        data=b"Second document",
    )
    run = grounded.admit(revisions=[grounded.source_lease.revision_id, second.revision_id])
    lease = await grounded.jobs.claim("chat", "compare-ledger")
    assert lease
    ctx = AuthContext(lease.actor_id, lease.workspace_id, "owner", 1, uuid4())
    db = engine(database["ed_app"])
    retriever = grounded.factory(ctx, lease).retriever
    executor = ToolExecutor(db, retriever)
    value = CompareInput(
        left_revision_id=grounded.source_lease.revision_id,
        right_revision_id=second.revision_id,
        focus="Fixture evidence",
    )
    calls = 0
    original = retriever.retrieve_revisions

    async def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if case == "failed_repeat":
            raise Problem(503, "fixture_index_down", "Fixture unavailable")
        return await original(*args, **kwargs)

    monkeypatch.setattr(retriever, "retrieve_revisions", counted)
    try:
        if case in ("query_spoof", "revision_spoof"):
            bad = (
                value.model_copy(update={"focus": "Different query"})
                if case == "query_spoof"
                else value.model_copy(update={"left_revision_id": uuid4()})
            )
            with pytest.raises(Problem, match="tool_scope_mismatch"):
                await executor.compare(ctx, lease, bad, "hybrid")
            assert calls == 0
            return
        if case == "failed_repeat":
            with pytest.raises(Problem, match="fixture_index_down"):
                await executor.compare(ctx, lease, value, "hybrid")
            with pytest.raises(Problem, match="tool_already_failed"):
                await executor.compare(ctx, lease, value, "hybrid")
            assert calls == 1
            return
        first = await executor.compare(ctx, lease, value, "hybrid")
        with psycopg.connect(database["operator"]) as conn:
            row = conn.execute(
                "SELECT execution_metadata->'comparison_read' FROM app.runs WHERE id=%s",
                (run["id"],),
            ).fetchone()[0]
            assert row["attempts"] == 1 and row["status"] == "succeeded"
            assert "Fixture evidence passage" not in str(row)
            if case == "source_revoked":
                conn.execute(
                    "UPDATE app.documents SET deleted_at=now() WHERE id=(SELECT document_id FROM app.document_revisions WHERE id=%s)",
                    (second.revision_id,),
                )
            elif case == "policy_changed":
                conn.execute(
                    "UPDATE app.workspaces SET version=version+1 WHERE id=%s", (lease.workspace_id,)
                )
            elif case == "payload_changed":
                conn.execute(
                    "UPDATE app.runs SET execution_metadata=jsonb_set(execution_metadata,'{comparison_read,payload_sha256}',to_jsonb(%s::text)) WHERE id=%s",
                    ("0" * 64, run["id"]),
                )
            elif case == "call_cap":
                conn.execute(
                    "UPDATE app.runs SET execution_metadata=jsonb_set(jsonb_set(execution_metadata,'{comparison_read,status}','\"running\"'),'{comparison_read,attempts}','3') WHERE id=%s",
                    (run["id"],),
                )
        if case == "replay":
            replay = await ToolExecutor(db, retriever).compare(ctx, lease, value, "hybrid")
            assert replay.evidence == first.evidence and replay.scope == first.scope
            assert calls == 1
        else:
            from evidencedesk.retrieval.models import ScopeChanged

            with pytest.raises((Problem, ScopeChanged)):
                await executor.compare(ctx, lease, value, "hybrid")
            assert calls == 1
    finally:
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.jobs SET status='cancelled',lease_until=NULL WHERE id=%s",
                (lease.job_id,),
            )
            conn.execute("UPDATE app.runs SET status='cancelled' WHERE id=%s", (run["id"],))
        await db.dispose()

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from evidencedesk.db import source_redaction_repository as repo
from evidencedesk.lifecycle.redaction import SourceRedactor

from tests.integration.answers.test_briefs import approval, proposal, work
from tests.integration.answers.test_execution import grounded as grounded
from tests.integration.retrieval.test_retrieval import publish_fixture

pytestmark = pytest.mark.integration


async def drain(h):
    redactor = SourceRedactor(h.runner.db)
    for _ in range(20):
        if not await redactor.prune_workspace(h.artifact.workspace_id):
            return
    raise AssertionError("Redaction failed to converge")


def delete_source(h):
    response = h.client.delete(h.base + f"/knowledge-bases/{h.artifact.kb_id}", headers=h.h)
    assert response.status_code == 202, response.text


@pytest.mark.parametrize("approved", [False, True])
async def test_deleted_source_content_is_physically_redacted_and_stays_unavailable(
    grounded, approved
):
    h = grounded
    run = h.admit()
    await h.execute()
    execution = proposal(h, run)
    await work(h)
    if approved:
        assert approval(h, execution).status_code == 200
        await work(h)
    redactor = SourceRedactor(h.runner.db)
    assert not await redactor.prune_workspace(h.artifact.workspace_id)
    delete_source(h)
    assert not await redactor.prune_workspace(uuid4())
    await drain(h)
    with psycopg.connect(h.database["operator"]) as conn:
        assert conn.execute(
            "SELECT text,answer,citations FROM app.messages WHERE id=%s", (run["message_id"],)
        ).fetchone() == ("", None, [])
        assert conn.execute(
            "SELECT count(*) FROM app.run_events WHERE run_id=%s", (run["id"],)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT response,input_tokens,output_tokens FROM app.model_usage WHERE run_id=%s",
            (run["id"],),
        ).fetchone() == (None, 900, 30)
        assert conn.execute(
            "SELECT arguments,result FROM app.tool_executions WHERE id=%s", (execution["id"],)
        ).fetchone() == ({}, None)
        if approved:
            assert conn.execute(
                "SELECT title,answer,citations FROM app.briefs WHERE execution_id=%s",
                (execution["id"],),
            ).fetchone() == ("", {}, [])
        # Even repairing/reintroducing a source cannot resurrect the erased answer.
        conn.execute(
            "UPDATE app.documents SET deleted_at=NULL WHERE id=%s", (h.artifact.document_id,)
        )
        conn.execute(
            "UPDATE app.knowledge_bases SET deleted_at=NULL WHERE id=%s", (h.artifact.kb_id,)
        )
        conn.execute(
            "UPDATE app.document_revisions SET status='published' WHERE id=%s",
            (h.artifact.revision_id,),
        )
    messages = h.client.get(h.base + f"/conversations/{run['conversation_id']}/messages").json()[
        "items"
    ]
    assistant = next(m for m in messages if m["id"] == run["message_id"])
    assert (
        assistant["source_status"] == "unavailable"
        and assistant["answer"] is None
        and assistant["text"] == ""
    )
    assert h.client.get(h.base + f"/runs/{run['id']}/events").status_code == 410
    detail = h.client.get(h.base + f"/tool-executions/{execution['id']}")
    assert detail.status_code == 200 and detail.json()["proposal"] is None
    assert approval(h, execution).status_code == 409
    assert not await redactor.prune_workspace(h.artifact.workspace_id)


async def test_redaction_follows_private_history_ancestry_preserves_independent_answer(
    grounded, object_store, database, services, tmp_path
):
    h = grounded
    first = h.admit()
    await h.execute()
    kb = h.client.post(
        h.base + "/knowledge-bases",
        json={"name": "Independent knowledge"},
        headers={**h.h, "Idempotency-Key": str(uuid4())},
    ).json()["id"]
    await publish_fixture(
        h.client,
        h.base + f"/knowledge-bases/{kb}/documents",
        h.h,
        database,
        object_store,
        services,
        tmp_path,
        data=b"Independent source",
    )
    h.kb = UUID(kb)
    child = h.admit(conversation=first["conversation_id"], version=2)
    await h.execute()
    descendant = h.admit(conversation=first["conversation_id"], version=3)
    await h.execute()
    independent = h.admit()
    await h.execute()
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.conversations SET summary=%s::jsonb WHERE id=%s",
            (json.dumps({"private": "Summary from source"}), first["conversation_id"]),
        )
    delete_source(h)
    await drain(h)
    with psycopg.connect(database["operator"]) as conn:
        for run in (first, child, descendant):
            assert conn.execute(
                "SELECT execution_metadata->>'source_redacted',execution_metadata->>'source_redaction_pending' FROM app.runs WHERE id=%s",
                (run["id"],),
            ).fetchone() == ("true", None)
            assert conn.execute(
                "SELECT text,answer FROM app.messages WHERE id=%s", (run["message_id"],)
            ).fetchone() == ("", None)
        assert conn.execute(
            "SELECT summary FROM app.conversations WHERE id=%s", (first["conversation_id"],)
        ).fetchone() == (None,)
        assert conn.execute(
            "SELECT answer IS NOT NULL FROM app.messages WHERE id=%s", (independent["message_id"],)
        ).fetchone() == (True,)


async def test_redaction_pages_and_transaction_failure_do_not_leave_partial_content_state(
    grounded, monkeypatch
):
    h = grounded
    run = h.admit()
    await h.execute()
    with psycopg.connect(h.database["operator"]) as conn:
        # Valid synthetic phase history exercises the physical deletion batch cap.
        for sequence in range(1000, 1205):
            conn.execute(
                "INSERT INTO app.run_events(workspace_id,run_id,sequence,type,data) VALUES(%s,%s,%s,'run.phase',%s::jsonb)",
                (h.artifact.workspace_id, run["id"], sequence, json.dumps({"phase": "validate"})),
            )
    delete_source(h)
    original = repo.step

    async def fail(*args):
        await original(*args)
        raise RuntimeError("Synthetic commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(repo, "step", fail)
        with pytest.raises(RuntimeError):
            await SourceRedactor(h.runner.db).prune_workspace(h.artifact.workspace_id)
    with psycopg.connect(h.database["operator"]) as conn:
        assert conn.execute(
            "SELECT answer IS NOT NULL FROM app.messages WHERE id=%s", (run["message_id"],)
        ).fetchone() == (True,)
        before = conn.execute(
            "SELECT count(*) FROM app.run_events WHERE run_id=%s", (run["id"],)
        ).fetchone()[0]
    assert await SourceRedactor(h.runner.db).prune_workspace(h.artifact.workspace_id)
    with psycopg.connect(h.database["operator"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.run_events WHERE run_id=%s", (run["id"],)
        ).fetchone() == (before - 100,)
    await drain(h)


async def test_source_redaction_actual_command_only_needs_application_database(grounded):
    h = grounded
    h.admit()
    await h.execute()
    delete_source(h)
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("ED_", "LANGCHAIN_", "LANGSMITH_"))
    }
    env["ED_DATABASE_URL"] = h.database["ed_app"]
    result = subprocess.run(
        [
            str(Path(sys.executable).with_name("evidencedesk")),
            "source-redact",
            "--workspace",
            str(h.artifact.workspace_id),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["processed"] == 1
    assert "Fixture evidence" not in result.stdout + result.stderr
    await drain(h)

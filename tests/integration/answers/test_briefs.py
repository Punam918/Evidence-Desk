import asyncio
import json
from uuid import uuid4

import psycopg
import pytest
from evidencedesk.db.session import engine
from evidencedesk.workflows.brief_runner import BriefRunner

from tests.integration.answers.test_execution import grounded as grounded
from tests.support import headers, sign_in

pytestmark = pytest.mark.integration


def proposal(h, run, *, title="Evidence brief"):
    result = h.client.post(
        h.base + f"/runs/{run['id']}/brief-proposals",
        json={"title": title, "answer_message_id": run["message_id"]},
        headers={**h.h, "Idempotency-Key": str(uuid4())},
    )
    assert result.status_code == 201, result.text
    return result.json()


def approval(h, execution, decision="approve", *, expected=None):
    return h.client.post(
        h.base + f"/tool-executions/{execution['id']}/approval",
        json={
            "decision": decision,
            "expected_payload_sha256": expected or execution["payload_sha256"],
        },
        headers={**h.h, "Idempotency-Key": str(uuid4())},
    )


async def work(h):
    lease = await h.jobs.claim("workflow", "brief-test")
    assert lease
    db = engine(h.database["ed_app"])
    try:
        await BriefRunner(db, h.jobs, h.database["ed_checkpoint"]).execute(lease, uuid4())
    finally:
        await db.dispose()
    return lease


def current(h, execution):
    response = h.client.get(h.base + f"/tool-executions/{execution['id']}")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("early", [False, True])
async def test_brief_durable_interrupt_and_early_decision_same_thread(grounded, database, early):
    run = grounded.admit()
    await grounded.execute()
    with psycopg.connect(database["operator"]) as conn:
        before = conn.execute(
            "SELECT count(*) FROM app.run_events WHERE run_id=%s", (run["id"],)
        ).fetchone()[0]
    execution = proposal(grounded, run)
    assert execution["status"] == "proposed" and execution["brief_id"] is None
    assert proposal(grounded, run)["id"] == execution["id"]
    if not early:
        await work(grounded)
        assert current(grounded, execution)["status"] == "awaiting_approval"
        assert await grounded.jobs.claim("workflow", "must-not-hold-slot") is None
    result = approval(grounded, execution)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "approved" and result.json()["brief_id"] is None
    await work(grounded)
    saved = current(grounded, execution)
    assert saved["status"] == "succeeded" and saved["brief_id"]
    assert approval(grounded, execution).json() == saved
    brief = grounded.client.get(grounded.base + f"/briefs/{saved['brief_id']}")
    assert brief.status_code == 200 and brief.json()["answer"]["status"] == "answered", brief.text
    assert len(grounded.client.get(grounded.base + "/briefs").json()["items"]) == 1
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM app.run_events WHERE run_id=%s", (run["id"],)
            ).fetchone()[0]
            == before
        )
        assert conn.execute(
            "SELECT count(*) FROM app.briefs WHERE execution_id=%s", (execution["id"],)
        ).fetchone() == (1,)
        threads = conn.execute(
            "SELECT id,graph_name FROM app.workflow_threads WHERE run_id=%s", (run["id"],)
        ).fetchall()
        assert len(threads) == 2 and {t[1] for t in threads} == {"answer_v1", "brief_v1"}
        thread = next(t[0] for t in threads if t[1] == "brief_v1")
        blobs = conn.execute(
            "SELECT blob FROM lg_checkpoints.checkpoint_blobs WHERE thread_id=%s", (str(thread),)
        ).fetchall()
        assert blobs and all(
            b"Fixture evidence passage" not in bytes(b[0] or b"")
            and b"Evidence brief" not in bytes(b[0] or b"")
            for b in blobs
        )


@pytest.mark.parametrize("boundary", ["proposal", "approved", "saved"])
@pytest.mark.parametrize("change", ["source", "role", "membership", "conversation", "policy"])
async def test_brief_current_authority_at_every_effect(grounded, database, boundary, change):
    run = grounded.admit()
    await grounded.execute()
    execution = proposal(grounded, run)
    if boundary in ("approved", "saved"):
        assert approval(grounded, execution).status_code == 200
    if boundary == "saved":
        await work(grounded)
    with psycopg.connect(database["operator"]) as conn:
        if change == "source":
            conn.execute(
                "UPDATE app.documents SET deleted_at=now() WHERE id=%s",
                (grounded.artifact.document_id,),
            )
        elif change == "role":
            conn.execute(
                "UPDATE app.memberships SET role='viewer' WHERE workspace_id=%s",
                (grounded.artifact.workspace_id,),
            )
        elif change == "membership":
            conn.execute(
                "DELETE FROM app.memberships WHERE workspace_id=%s",
                (grounded.artifact.workspace_id,),
            )
        elif change == "conversation":
            conn.execute(
                "UPDATE app.conversations SET deleted_at=now() WHERE id=%s",
                (run["conversation_id"],),
            )
        else:
            conn.execute(
                "UPDATE app.workspaces SET version=version+1 WHERE id=%s",
                (grounded.artifact.workspace_id,),
            )
    if boundary == "proposal":
        assert approval(grounded, execution).status_code in (403, 404, 409)
    elif boundary == "approved":
        await work(grounded)
    else:
        response = grounded.client.get(grounded.base + f"/tool-executions/{execution['id']}")
        if change in ("membership", "conversation"):
            assert response.status_code == 404
        elif change == "source":
            assert response.json()["proposal"] is None
            brief = grounded.client.get(
                grounded.base + f"/briefs/{response.json()['brief_id']}"
            ).json()
            assert brief["answer"] is None and brief["citations"] == []
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.briefs WHERE execution_id=%s", (execution["id"],)
        ).fetchone() == (int(boundary == "saved"),)
        # Cancel an intentionally unprocessed proposal so this case cannot be claimed by another.
        conn.execute(
            "UPDATE app.jobs SET status='cancelled' WHERE kind='workflow' AND run_id=%s AND status IN ('queued','retry_wait')",
            (run["id"],),
        )


@pytest.mark.parametrize("decision", ["reject", "approve"])
async def test_brief_expiry_hash_and_conflicting_decisions(grounded, database, decision):
    run = grounded.admit()
    await grounded.execute()
    execution = proposal(grounded, run)
    assert approval(grounded, execution, expected="0" * 64).status_code == 409
    assert approval(grounded, execution, decision).status_code == 200
    assert (
        approval(grounded, execution, "approve" if decision == "reject" else "reject").status_code
        == 409
    )
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.tool_executions SET approval_expires_at=now()-interval '1 second' WHERE id=%s",
            (execution["id"],),
        )
    await work(grounded)
    assert current(grounded, execution)["status"] == (
        "rejected" if decision == "reject" else "expired"
    )
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.briefs WHERE execution_id=%s", (execution["id"],)
        ).fetchone() == (0,)


async def test_brief_approval_race_creates_one_artifact(grounded, database):
    run = grounded.admit()
    await grounded.execute()
    execution = proposal(grounded, run)
    results = await asyncio.gather(
        *(asyncio.to_thread(approval, grounded, execution) for _ in range(4))
    )
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    await work(grounded)
    assert current(grounded, execution)["status"] == "succeeded"
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.briefs WHERE execution_id=%s", (execution["id"],)
        ).fetchone() == (1,)


async def test_briefs_are_creator_private_even_from_admin(grounded, database, auth_client):
    run = grounded.admit()
    await grounded.execute()
    execution = proposal(grounded, run)
    approval(grounded, execution)
    await work(grounded)
    saved = current(grounded, execution)
    client, state = auth_client
    state["subject"] = str(uuid4())
    other = sign_in(client, state)
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.memberships(workspace_id,user_id,role) VALUES(%s,%s,'admin')",
            (grounded.artifact.workspace_id, other["user"]["id"]),
        )
    for path in (
        f"/tool-executions/{execution['id']}",
        f"/briefs/{saved['brief_id']}",
        f"/runs/{run['id']}/tool-executions",
    ):
        assert client.get(grounded.base + path).status_code == 404
    assert (
        client.post(
            grounded.base + f"/tool-executions/{execution['id']}/approval",
            json={"decision": "approve", "expected_payload_sha256": execution["payload_sha256"]},
            headers=headers(other),
        ).status_code
        == 404
    )
    assert client.get(grounded.base + "/briefs").json()["items"] == []


@pytest.mark.parametrize("node", ["prepare", "approval", "reauthorize", "save"])
@pytest.mark.parametrize("boundary", ["before", "after"])
async def test_brief_restart_at_each_graph_boundary(
    grounded, database, monkeypatch, node, boundary
):
    from evidencedesk.workflows.brief_runner import BriefSteps

    run = grounded.admit()
    await grounded.execute()
    execution = proposal(grounded, run)
    assert approval(grounded, execution).status_code == 200
    original = BriefSteps.perform
    once = False

    async def crash(self, name, state):
        nonlocal once
        if name == node and not once and boundary == "before":
            once = True
            raise RuntimeError("simulated process crash")
        result = await original(self, name, state)
        if name == node and not once and boundary == "after":
            once = True
            raise RuntimeError("simulated process crash")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(BriefSteps, "perform", crash)
        with pytest.raises(RuntimeError, match="simulated"):
            await work(grounded)
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.jobs SET lease_until=now()-interval '1 second' WHERE kind='workflow' AND run_id=%s AND status='running'",
            (run["id"],),
        )
    await grounded.jobs.sweep()
    await work(grounded)
    assert current(grounded, execution)["status"] == "succeeded"
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.briefs WHERE execution_id=%s", (execution["id"],)
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "change", ["title", "message", "answer", "actor", "hash", "version", "expiry", "approved_by"]
)
async def test_brief_frozen_payload_tampering_prevents_effect(grounded, database, change):
    from psycopg.types.json import Jsonb

    run = grounded.admit()
    await grounded.execute()
    execution = proposal(grounded, run)
    assert approval(grounded, execution).status_code == 200
    with psycopg.connect(database["operator"]) as conn:
        frozen = conn.execute(
            "SELECT arguments FROM app.tool_executions WHERE id=%s", (execution["id"],)
        ).fetchone()[0]
        if change == "title":
            frozen["proposal"]["title"] = "Different title"
        elif change == "message":
            frozen["proposal"]["answer_message_id"] = str(uuid4())
        elif change == "actor":
            frozen["user_id"] = str(uuid4())
        elif change == "answer":
            conn.execute(
                "UPDATE app.messages SET answer=jsonb_set(answer,'{limitations}','[\"Changed\"]') WHERE id=%s",
                (run["message_id"],),
            )
        elif change == "hash":
            conn.execute(
                "UPDATE app.tool_executions SET payload_sha256=%s WHERE id=%s",
                ("0" * 64, execution["id"]),
            )
        elif change == "version":
            conn.execute(
                "UPDATE app.tool_executions SET tool_version='9' WHERE id=%s", (execution["id"],)
            )
        elif change == "expiry":
            conn.execute(
                "UPDATE app.tool_executions SET approval_expires_at=approval_expires_at+interval '1 hour' WHERE id=%s",
                (execution["id"],),
            )
        elif change == "approved_by":
            conn.execute(
                "UPDATE app.tool_executions SET approved_by=NULL WHERE id=%s", (execution["id"],)
            )
        conn.execute(
            "UPDATE app.tool_executions SET arguments=%s WHERE id=%s",
            (Jsonb(frozen), execution["id"]),
        )
    await work(grounded)
    assert current(grounded, execution)["status"] == "failed"
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.briefs WHERE execution_id=%s", (execution["id"],)
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "invalid",
    ["empty", "blank", "long", "extra", "bad_uuid", "missing_message", "number", "surrogate"],
)
async def test_invalid_brief_proposals_never_queue_effects(grounded, database, invalid):
    run = grounded.admit()
    await grounded.execute()
    body = {"title": "Brief", "answer_message_id": run["message_id"]}
    if invalid == "empty":
        body["title"] = ""
    elif invalid == "blank":
        body["title"] = "  "
    elif invalid == "long":
        body["title"] = "x" * 121
    elif invalid == "extra":
        body["workspace_id"] = str(uuid4())
    elif invalid == "bad_uuid":
        body["answer_message_id"] = "bad"
    elif invalid == "missing_message":
        del body["answer_message_id"]
    elif invalid == "number":
        body["title"] = 42
    else:
        body["title"] = "\ud800"
    response = grounded.client.post(
        grounded.base + f"/runs/{run['id']}/brief-proposals",
        content=json.dumps(body),
        headers={**grounded.h, "Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.tool_executions WHERE run_id=%s", (run["id"],)
        ).fetchone() == (0,)


async def test_brief_delete_pagination_idempotency_and_expiry_recovery(grounded, database):
    run = grounded.admit()
    await grounded.execute()
    executions = [proposal(grounded, run, title=f"Brief {i}") for i in range(3)]
    for execution in executions[:2]:
        approval(grounded, execution)
        await work(grounded)
    page = grounded.client.get(grounded.base + "/briefs?limit=1").json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    next_page = grounded.client.get(
        grounded.base + "/briefs", params={"limit": 1, "cursor": page["next_cursor"]}
    ).json()
    assert next_page["items"][0]["id"] != page["items"][0]["id"]
    assert (
        grounded.client.get(
            grounded.base + f"/runs/{run['id']}/tool-executions",
            params={"cursor": page["next_cursor"]},
        ).status_code
        == 422
    )
    uid = page["items"][0]["id"]
    assert (
        grounded.client.delete(grounded.base + f"/briefs/{uid}", headers=grounded.h).status_code
        == 204
    )
    assert grounded.client.get(grounded.base + f"/briefs/{uid}").status_code == 404
    await work(grounded)  # Last proposal reaches durable interrupt without approval.
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.tool_executions SET approval_expires_at=now()-interval '1 second' WHERE id=%s",
            (executions[2]["id"],),
        )
        conn.execute(
            "UPDATE app.jobs SET available_at=now() WHERE run_id=%s AND kind='workflow' AND status='retry_wait'",
            (run["id"],),
        )
    await work(grounded)
    assert current(grounded, executions[2])["status"] == "expired"


async def test_checkpoint_failure_before_effect_recovers_from_business_approval(
    grounded, database, monkeypatch
):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    run = grounded.admit()
    await grounded.execute()
    execution = proposal(grounded, run)
    approval(grounded, execution)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("checkpoint unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncPostgresSaver, "aput", unavailable)
        with pytest.raises(RuntimeError, match="checkpoint unavailable"):
            await work(grounded)
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.briefs WHERE execution_id=%s", (execution["id"],)
        ).fetchone() == (0,)
        conn.execute(
            "UPDATE app.jobs SET lease_until=now()-interval '1 second' WHERE kind='workflow' AND run_id=%s AND status='running'",
            (run["id"],),
        )
    await grounded.jobs.sweep()
    await work(grounded)
    assert current(grounded, execution)["status"] == "succeeded"


async def test_brief_thread_gate_prevents_competing_invocation(grounded, database):
    from evidencedesk.workflows.thread_gate import thread_gate

    run = grounded.admit()
    await grounded.execute()
    execution = proposal(grounded, run)
    approval(grounded, execution)
    with psycopg.connect(database["operator"]) as conn:
        thread = conn.execute(
            "SELECT id FROM app.workflow_threads WHERE tool_execution_id=%s", (execution["id"],)
        ).fetchone()[0]
    async with thread_gate(database["ed_checkpoint"], thread):
        await work(grounded)
    assert current(grounded, execution)["status"] == "approved"
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.jobs SET available_at=now() WHERE kind='workflow' AND run_id=%s",
            (run["id"],),
        )
    await work(grounded)
    assert current(grounded, execution)["status"] == "succeeded"


async def test_resume_payload_is_not_approval(grounded, database):
    from evidencedesk.db.session import AuthContext
    from evidencedesk.errors import Problem
    from evidencedesk.workflows.brief import build_brief_graph
    from evidencedesk.workflows.brief_runner import BriefSteps
    from evidencedesk.workflows.checkpoint_codec import CheckpointCodec
    from evidencedesk.workflows.state import ReferenceState
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.types import Command
    from psycopg.rows import dict_row

    run = grounded.admit()
    await grounded.execute()
    execution = proposal(grounded, run)
    await work(grounded)
    with psycopg.connect(database["operator"]) as conn:
        thread, actor = conn.execute(
            "SELECT id,user_id FROM app.workflow_threads WHERE tool_execution_id=%s",
            (execution["id"],),
        ).fetchone()
        conn.execute(
            "UPDATE app.jobs SET available_at=now() WHERE kind='workflow' AND run_id=%s",
            (run["id"],),
        )
    lease = await grounded.jobs.claim("workflow", "forged-resume")
    assert lease
    db = engine(database["ed_app"])
    expected = ReferenceState(
        thread_id=thread,
        run_id=run["id"],
        intent="brief",
        revision_ids=[],
        evidence_chunk_ids=[],
        tool_execution_id=execution["id"],
        step="prepare",
        config_sha256=execution["payload_sha256"],
    )
    try:
        async with await psycopg.AsyncConnection.connect(
            database["ed_checkpoint"], autocommit=True, row_factory=dict_row
        ) as conn:
            saver = AsyncPostgresSaver(conn, serde=CheckpointCodec())
            graph = build_brief_graph(
                BriefSteps(
                    db,
                    AuthContext(actor, grounded.artifact.workspace_id, "owner", 1, uuid4()),
                    lease,
                    expected,
                ),
                saver,
            )
            with pytest.raises(Problem, match="approval_required"):
                await graph.ainvoke(
                    Command(resume={"decision": "approve", "approved_by": str(actor)}),
                    {"configurable": {"thread_id": str(thread)}},
                    durability="sync",
                )
    finally:
        await db.dispose()
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.jobs SET status='cancelled',lease_until=NULL WHERE kind='workflow' AND run_id=%s",
                (run["id"],),
            )
            assert conn.execute(
                "SELECT count(*) FROM app.briefs WHERE execution_id=%s", (execution["id"],)
            ).fetchone() == (0,)


async def test_brief_csrf_and_idempotency_bind_body(grounded):
    run = grounded.admit()
    await grounded.execute()
    url = grounded.base + f"/runs/{run['id']}/brief-proposals"
    body = {"title": "First", "answer_message_id": run["message_id"]}
    assert grounded.client.post(url, json=body).status_code == 403
    h = {**grounded.h, "Idempotency-Key": str(uuid4())}
    first = grounded.client.post(url, json=body, headers=h)
    assert first.status_code == 201, first.text
    assert grounded.client.post(url, json=body, headers=h).json() == first.json()
    assert (
        grounded.client.post(url, json={**body, "title": "Changed"}, headers=h).status_code == 409
    )
    assert approval(grounded, first.json(), "reject").status_code == 200
    await work(grounded)


async def test_real_brief_worker_command_saves_without_model_secrets(grounded, database):
    import os

    run = grounded.admit()
    await grounded.execute()
    execution = proposal(grounded, run)
    approval(grounded, execution)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ED_")}
    env.update(
        ED_DATABASE_URL=database["ed_app"],
        ED_DISPATCH_DATABASE_URL=database["ed_dispatch"],
        ED_CHECKPOINT_DATABASE_URL=database["ed_checkpoint"],
    )
    process = await asyncio.create_subprocess_exec(
        ".venv/bin/evidencedesk",
        "brief-worker",
        "--once",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode == 0, (stdout.decode(), stderr.decode())
    assert current(grounded, execution)["status"] == "succeeded"

from uuid import UUID

import psycopg
import pytest
from evidencedesk.errors import LeaseLost
from evidencedesk.lifecycle.conversations import ConversationPurger

from tests.integration.answers.test_briefs import approval, proposal, work
from tests.integration.answers.test_execution import grounded as grounded

pytestmark = pytest.mark.integration


def delete_conversation(h, run):
    response = h.client.delete(h.base + f"/conversations/{run['conversation_id']}", headers=h.h)
    assert response.status_code == 202, response.text
    assert h.client.get(h.base + f"/runs/{run['id']}/events").status_code == 404
    return UUID(response.json()["job_id"])


async def page(h, job):
    with psycopg.connect(h.database["operator"]) as conn:
        conn.execute("UPDATE app.jobs SET available_at=now() WHERE id=%s", (job,))
    lease = await h.jobs.claim_conversation_purge("native-purge")
    assert lease and lease.job_id == job
    await ConversationPurger(h.runner.db, h.jobs, h.database["ed_checkpoint"]).run(lease)


async def drain(h, job):
    for _ in range(10):
        await page(h, job)
        with psycopg.connect(h.database["operator"]) as conn:
            if conn.execute("SELECT status FROM app.jobs WHERE id=%s", (job,)).fetchone() == (
                "succeeded",
            ):
                return
    raise AssertionError("Purge did not converge")


async def test_delete_during_native_response_fences_late_output_without_reissuing(grounded):
    h = grounded
    run = h.admit()
    deleted = []

    async def while_transport_issued():
        job = delete_conversation(h, run)
        deleted.append(job)
        await page(h, job)  # Remove cache page.
        await page(h, job)  # Revoke worker/settle; active graph still owns its gate.
        with psycopg.connect(h.database["operator"]) as conn:
            assert conn.execute(
                "SELECT status FROM app.runs WHERE id=%s", (run["id"],)
            ).fetchone() == ("cancelled",)
            assert conn.execute(
                "SELECT count(*) FROM app.run_events WHERE run_id=%s AND type='answer.completed'",
                (run["id"],),
            ).fetchone() == (0,)

    h.observed["hook"] = while_transport_issued
    with pytest.raises(LeaseLost):
        await h.execute()
    await drain(h, deleted[0])
    assert len(h.observed["calls"]) == 1
    with psycopg.connect(h.database["operator"]) as conn:
        assert conn.execute(
            "SELECT state,usage_estimated,response,run_id,conversation_id FROM app.model_usage WHERE workspace_id=%s",
            (h.artifact.workspace_id,),
        ).fetchone() == ("estimated", True, None, None, None)
        assert conn.execute(
            "SELECT count(*) FROM app.documents WHERE id=%s", (h.artifact.document_id,)
        ).fetchone() == (1,)


@pytest.mark.parametrize("approved", [False, True])
async def test_real_interrupted_or_saved_brief_is_erased_without_replaying_approval(
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
    job = delete_conversation(h, run)
    assert approval(h, execution).status_code == 404
    await drain(h, job)
    assert len(h.observed["calls"]) == 1
    with psycopg.connect(h.database["operator"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM app.idempotency_keys WHERE workspace_id=%s AND response_body->>'id'=%s",
            (h.artifact.workspace_id, execution["id"]),
        ).fetchone() == (0,)
        for table in ("runs", "messages", "briefs", "tool_executions", "workflow_threads"):
            assert conn.execute(
                f"SELECT count(*) FROM app.{table} WHERE workspace_id=%s",
                (h.artifact.workspace_id,),
            ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM app.deletion_tombstones WHERE workspace_id=%s AND entity_type='workflow_thread' AND purged_at IS NOT NULL",
            (h.artifact.workspace_id,),
        ).fetchone() == (2,)

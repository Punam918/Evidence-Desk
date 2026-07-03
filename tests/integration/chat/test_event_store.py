import asyncio
from uuid import UUID, uuid4

import psycopg
import pytest
from evidencedesk.chat.models import Answer
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import AuthContext, engine
from evidencedesk.errors import LeaseLost
from evidencedesk.events.models import Completed, Phase
from evidencedesk.events.store import EventStore

from tests.support import headers, sign_in

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_fenced_completion_commits_one_terminal_and_cancel_wins(
    auth_client, database, cancel_first
):
    client, state = auth_client
    identity = sign_in(client, state)
    h = headers(identity)
    workspace = client.post("/v1/workspaces", json={"name": "Worker"}, headers=h).json()["id"]
    base = f"/v1/workspaces/{workspace}"
    conversation = client.post(
        base + "/conversations", json={"title": "Worker finalization"}, headers=h
    ).json()["id"]
    kb = uuid4()
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.knowledge_bases(workspace_id,id,name,created_by) VALUES(%s,%s,%s,%s)",
            (workspace, kb, "Sources", identity["user"]["id"]),
        )
    body = {
        "query": "What is documented?",
        "kb_ids": [str(kb)],
        "model_key": "local_standard",
        "intent": "ask",
        "expected_conversation_version": 1,
    }
    run = client.post(base + f"/conversations/{conversation}/runs", json=body, headers=h).json()
    app, dispatch = engine(database["ed_app"]), engine(database["ed_dispatch"])
    try:
        lease = await JobRepository(dispatch).claim("chat", "event-test")
        assert lease and str(lease.run_id) == run["id"]
        ctx = AuthContext(lease.actor_id, lease.workspace_id, "owner", 1, uuid4())
        store = EventStore(app)
        await store.append(ctx, lease, Phase(phase="retrieving", degraded=[]))
        if cancel_first:
            assert (
                client.post(
                    base + f"/runs/{run['id']}/cancel", headers=headers(identity)
                ).status_code
                == 202
            )
        completion = Completed(
            message_id=UUID(run["message_id"]),
            answer=Answer(status="abstained", sentences=[], limitations=["No supporting source."]),
            citations=[],
        )
        results = await asyncio.gather(
            store.append(ctx, lease, completion),
            store.append(ctx, lease, completion),
            return_exceptions=True,
        )
        assert sum(isinstance(item, LeaseLost) for item in results) == 1, results
        events = client.get(base + f"/runs/{run['id']}/events")
        assert events.status_code == 200, events.text
        assert (
            events.text.count("event: run.cancelled" if cancel_first else "event: answer.completed")
            == 1
        )
        with psycopg.connect(database["operator"]) as conn:
            assert conn.execute("SELECT active_runs FROM app.admission_counters").fetchone()[0] == 0
            assert (
                conn.execute(
                    "SELECT status FROM app.jobs WHERE workspace_id=%s AND id=%s",
                    (workspace, lease.job_id),
                ).fetchone()[0]
                == "succeeded"
            )
        saved = client.get(base + f"/conversations/{conversation}/messages").json()["items"][-1]
        assert saved["state"] == ("cancelled" if cancel_first else "final")
    finally:
        await app.dispose()
        await dispatch.dispose()

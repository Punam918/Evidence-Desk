from uuid import UUID, uuid4

import psycopg
import pytest
from evidencedesk.chat.models import Answer
from evidencedesk.db.identity_repository import IdentityRepository
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import engine
from evidencedesk.events.models import Completed, Failed, Phase
from evidencedesk.events.store import EventStore
from evidencedesk.workflows.runner import WorkflowRunner

from tests.support import headers, sign_in

pytestmark = pytest.mark.integration


async def test_leased_graph_executes_through_real_event_store(auth_client, database):
    client, state = auth_client
    identity = sign_in(client, state)
    h = headers(identity)
    workspace = client.post("/v1/workspaces", json={"name": "Graph worker"}, headers=h).json()["id"]
    base = f"/v1/workspaces/{workspace}"
    conversation = client.post(base + "/conversations", json={"title": "Graph"}, headers=h).json()[
        "id"
    ]
    kb = uuid4()
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.knowledge_bases(workspace_id,id,name,created_by) VALUES(%s,%s,%s,%s)",
            (workspace, kb, "Sources", identity["user"]["id"]),
        )
    response = client.post(
        base + f"/conversations/{conversation}/runs",
        json={
            "query": "A private question that must not enter checkpoints.",
            "kb_ids": [str(kb)],
            "model_key": "local_standard",
            "intent": "ask",
            "expected_conversation_version": 1,
        },
        headers=h,
    )
    assert response.status_code == 202, response.text
    run = response.json()
    db, dispatch, identity_db = (
        engine(database["ed_app"]),
        engine(database["ed_dispatch"]),
        engine(database["ed_identity"]),
    )
    calls = []

    class FixtureSteps:
        def __init__(self, ctx, lease):
            self.ctx, self.lease, self.error = ctx, lease, None

        async def perform(self, node, state):
            calls.append(node)
            if node == "retrieve":
                await EventStore(db).append(
                    self.ctx, self.lease, Phase(phase="retrieving", degraded=[])
                )
            if node == "finalize":
                await EventStore(db).append(
                    self.ctx,
                    self.lease,
                    Completed(
                        message_id=UUID(run["message_id"]),
                        answer=Answer(
                            status="abstained", sentences=[], limitations=["No fixture sources."]
                        ),
                        citations=[],
                    ),
                )
            if node == "terminalize":
                await EventStore(db).append(
                    self.ctx,
                    self.lease,
                    Failed(code=self.error.code, retryable=False, discard_draft=True),
                )
            return state.model_copy(
                update={"step": "done" if node in ("finalize", "terminalize") else node}
            )

        async def record_failure(self, state, error):
            self.error = error

    try:
        jobs = JobRepository(dispatch)
        lease = await jobs.claim("chat", "graph-worker")
        assert lease and str(lease.run_id) == run["id"]
        runner = WorkflowRunner(
            db, IdentityRepository(identity_db), jobs, database["ed_checkpoint"]
        )
        await runner.execute(lease, uuid4(), FixtureSteps)
        assert calls == ["authorize", "route", "retrieve", "abstain", "finalize"]
        assert client.get(base + f"/runs/{run['id']}").json()["status"] == "completed"
        assert "answer.completed" in client.get(base + f"/runs/{run['id']}/events").text
        with psycopg.connect(database["operator"]) as conn:
            assert (
                conn.execute(
                    "SELECT status FROM app.workflow_threads WHERE workspace_id=%s AND run_id=%s",
                    (workspace, run["id"]),
                ).fetchone()[0]
                == "completed"
            )
            blobs = conn.execute(
                "SELECT blob FROM lg_checkpoints.checkpoint_blobs WHERE thread_id=(SELECT id::text FROM app.workflow_threads WHERE workspace_id=%s AND run_id=%s)",
                (workspace, run["id"]),
            ).fetchall()
            assert blobs and all(b"private question" not in (blob or b"") for (blob,) in blobs)
    finally:
        await db.dispose()
        await dispatch.dispose()
        await identity_db.dispose()


@pytest.mark.parametrize("condition", ["cancel", "deadline", "issued"])
async def test_runner_preflight_terminalizes_without_invoking_nodes(
    auth_client, database, condition
):
    from datetime import UTC, datetime

    client, state = auth_client
    identity = sign_in(client, state)
    h = headers(identity)
    workspace = client.post("/v1/workspaces", json={"name": "Recovery guard"}, headers=h).json()[
        "id"
    ]
    base = f"/v1/workspaces/{workspace}"
    conversation = client.post(base + "/conversations", json={"title": "Guard"}, headers=h).json()[
        "id"
    ]
    kb = uuid4()
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.knowledge_bases(workspace_id,id,name,created_by) VALUES(%s,%s,%s,%s)",
            (workspace, kb, "Sources", identity["user"]["id"]),
        )
    run = client.post(
        base + f"/conversations/{conversation}/runs",
        json={
            "query": "Question",
            "kb_ids": [str(kb)],
            "model_key": "local_standard",
            "intent": "ask",
            "expected_conversation_version": 1,
        },
        headers=h,
    ).json()
    with psycopg.connect(database["operator"]) as conn:
        if condition == "cancel":
            conn.execute(
                "UPDATE app.runs SET cancel_requested=true WHERE workspace_id=%s AND id=%s",
                (workspace, run["id"]),
            )
        elif condition == "deadline":
            conn.execute(
                "UPDATE app.runs SET created_at=now()-interval '61 seconds' WHERE workspace_id=%s AND id=%s",
                (workspace, run["id"]),
            )
        else:
            day = datetime.now(UTC).date()
            conn.execute(
                "INSERT INTO app.model_usage(workspace_id,id,run_id,user_id,conversation_id,attempt_no,purpose,model_key,resolved_model_id,price_version,reserved_micro_usd,execution_state,state,day_start,month_start) VALUES(%s,%s,%s,%s,%s,1,'generation','fixture','fixture','v1',10000,'issued','reserved',%s,%s)",
                (
                    workspace,
                    uuid4(),
                    run["id"],
                    identity["user"]["id"],
                    conversation,
                    day,
                    day.replace(day=1),
                ),
            )
            conn.execute(
                "UPDATE app.budget_windows SET reserved_micro_usd=10000 WHERE workspace_id=%s",
                (workspace,),
            )
    db, dispatch, identity_db = (
        engine(database["ed_app"]),
        engine(database["ed_dispatch"]),
        engine(database["ed_identity"]),
    )

    def forbidden_factory(ctx, lease):
        raise AssertionError("Preflight must reject before constructing execution steps")

    try:
        jobs = JobRepository(dispatch)
        lease = await jobs.claim("chat", "guard-worker")
        assert lease
        await WorkflowRunner(
            db, IdentityRepository(identity_db), jobs, database["ed_checkpoint"]
        ).execute(lease, uuid4(), forbidden_factory)
        saved = client.get(base + f"/runs/{run['id']}").json()
        assert saved["status"] == (
            "cancelled"
            if condition == "cancel"
            else "interrupted"
            if condition == "issued"
            else "failed"
        )
        with psycopg.connect(database["operator"]) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM app.run_events WHERE workspace_id=%s AND run_id=%s AND type IN ('run.failed','run.cancelled')",
                    (workspace, run["id"]),
                ).fetchone()[0]
                == 1
            )
            if condition == "issued":
                assert conn.execute(
                    "SELECT state,execution_state,cost_micro_usd FROM app.model_usage WHERE workspace_id=%s",
                    (workspace,),
                ).fetchone() == ("estimated", "ambiguous", 10000)
    finally:
        await db.dispose()
        await dispatch.dispose()
        await identity_db.dispose()

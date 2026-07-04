from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import engine

pytestmark = pytest.mark.integration


def seed_run(conn, workspace, actor, run, state):
    conversation, message, job = uuid4(), uuid4(), uuid4()
    conn.execute(
        "INSERT INTO app.conversations(workspace_id,id,user_id,title) VALUES(%s,%s,%s,%s)",
        (workspace, conversation, actor, "Recovery"),
    )
    conn.execute(
        "INSERT INTO app.messages(workspace_id,id,conversation_id,user_id,sequence,role,state,text) VALUES(%s,%s,%s,%s,1,'user','final','fixture')",
        (workspace, message, conversation, actor),
    )
    conn.execute(
        "INSERT INTO app.runs(workspace_id,id,conversation_id,user_id,user_message_id,status,intent,requested_model_key,config_sha256,prompt_version) VALUES(%s,%s,%s,%s,%s,'running','ask','fixture','fixture','v1')",
        (workspace, run, conversation, actor, message),
    )
    conn.execute(
        "INSERT INTO app.jobs(workspace_id,id,kind,status,run_id,requested_by,attempts,lease_token,lease_until,dedupe_key) VALUES(%s,%s,'chat','running',%s,%s,1,1,now()-interval '1 second',%s)",
        (workspace, job, run, actor, str(job)),
    )
    if state:
        day = datetime.now(UTC).date()
        month = day.replace(day=1)
        conn.execute(
            "INSERT INTO app.model_usage(workspace_id,id,run_id,user_id,conversation_id,attempt_no,purpose,model_key,resolved_model_id,price_version,reserved_micro_usd,execution_state,state,day_start,month_start) VALUES(%s,%s,%s,%s,%s,1,'generation','fixture','fixture','v1',10000,%s,'reserved',%s,%s)",
            (workspace, uuid4(), run, actor, conversation, state, day, month),
        )
        for period, date in [("day", day), ("month", month)]:
            conn.execute(
                "INSERT INTO app.budget_windows(workspace_id,period,starts_at,limit_micro_usd,reserved_micro_usd) VALUES(%s,%s,%s,100000,10000)",
                (workspace, period, date),
            )
    return job


@pytest.fixture
def recovery_workspace(database):
    workspace, actor = uuid4(), uuid4()
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.users(id,issuer,subject,email,display_name) VALUES(%s,%s,%s,%s,%s)",
            (actor, "recovery", str(actor), f"{actor}@example.test", "Actor"),
        )
        conn.execute("INSERT INTO app.workspaces(id,name) VALUES(%s,%s)", (workspace, "Recovery"))
        conn.execute(
            "INSERT INTO app.memberships(workspace_id,user_id,role) VALUES(%s,%s,'owner')",
            (workspace, actor),
        )
    yield workspace, actor
    with psycopg.connect(database["operator"]) as conn:
        conn.execute("DELETE FROM app.workspaces WHERE id=%s", (workspace,))


@pytest.mark.parametrize("execution", ["issued", "ambiguous"])
async def test_uncertain_attempt_never_replays_even_without_draft(
    database, recovery_workspace, execution
):
    workspace, actor = recovery_workspace
    run = uuid4()
    with psycopg.connect(database["operator"]) as conn:
        job = seed_run(conn, workspace, actor, run, execution)
    dispatch = engine(database["ed_dispatch"])
    repository = JobRepository(dispatch)
    try:
        await repository.sweep()
        await (
            repository.sweep()
        )  # Restarting recovery must not duplicate charges or terminal events.
        with psycopg.connect(database["operator"]) as conn:
            assert (
                conn.execute(
                    "SELECT status FROM app.runs WHERE workspace_id=%s AND id=%s", (workspace, run)
                ).fetchone()[0]
                == "interrupted"
            )
            assert (
                conn.execute(
                    "SELECT status FROM app.jobs WHERE workspace_id=%s AND id=%s", (workspace, job)
                ).fetchone()[0]
                == "dead"
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM app.run_events WHERE workspace_id=%s AND run_id=%s",
                    (workspace, run),
                ).fetchone()[0]
                == 1
            )
            assert conn.execute(
                "SELECT reserved_micro_usd,spent_micro_usd FROM app.budget_windows WHERE workspace_id=%s",
                (workspace,),
            ).fetchall() == [(0, 10000), (0, 10000)]
            assert conn.execute(
                "SELECT execution_state,state,cost_micro_usd FROM app.model_usage WHERE workspace_id=%s",
                (workspace,),
            ).fetchone() == ("ambiguous", "estimated", 10000)
    finally:
        await dispatch.dispose()


@pytest.mark.parametrize("execution", [None, "response_stored"])
async def test_unissued_or_stored_response_can_resume(database, recovery_workspace, execution):
    workspace, actor = recovery_workspace
    with psycopg.connect(database["operator"]) as conn:
        job = seed_run(conn, workspace, actor, uuid4(), execution)
    dispatch = engine(database["ed_dispatch"])
    try:
        await JobRepository(dispatch).sweep()
        with psycopg.connect(database["operator"]) as conn:
            assert (
                conn.execute(
                    "SELECT status FROM app.jobs WHERE workspace_id=%s AND id=%s", (workspace, job)
                ).fetchone()[0]
                == "retry_wait"
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM app.run_events WHERE workspace_id=%s", (workspace,)
                ).fetchone()[0]
                == 0
            )
    finally:
        await dispatch.dispose()


def test_parallel_admission_respects_global_and_workspace_caps(database):
    from concurrent.futures import ThreadPoolExecutor

    actor = uuid4()
    workspaces = [uuid4() for _ in range(4)]
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.users(id,issuer,subject,email,display_name) VALUES(%s,%s,%s,%s,%s)",
            (actor, "admission", str(actor), f"{actor}@example.test", "Actor"),
        )
        for workspace in workspaces:
            conn.execute(
                "INSERT INTO app.workspaces(id,name) VALUES(%s,%s)", (workspace, "Admission")
            )
            conn.execute(
                "INSERT INTO app.memberships(workspace_id,user_id,role) VALUES(%s,%s,'owner')",
                (workspace, actor),
            )

    def admit(workspace):
        with psycopg.connect(database["ed_app"]) as conn:
            conn.execute(
                "SELECT set_config('app.workspace_id',%s,true),set_config('app.user_id',%s,true)",
                (str(workspace), str(actor)),
            )
            allowed = conn.execute("SELECT app.reserve_run_slot()").fetchone()[0]
            if allowed:
                seed_run(conn, workspace, actor, uuid4(), None)
            return allowed

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(admit, workspaces * 5))
        assert sum(results) == 5
        with psycopg.connect(database["operator"]) as conn:
            counts = conn.execute(
                "SELECT count(*) FROM app.runs WHERE workspace_id=ANY(%s) GROUP BY workspace_id",
                (workspaces,),
            ).fetchall()
            assert all(count <= 2 for (count,) in counts)
    finally:
        with psycopg.connect(database["operator"]) as conn:
            conn.execute("DELETE FROM app.workspaces WHERE id=ANY(%s)", (workspaces,))

from uuid import uuid4

import psycopg
import pytest

from tests.integration.answers.test_execution import grounded as grounded
from tests.support import headers, sign_in

pytestmark = pytest.mark.integration


async def test_feedback_is_private_idempotent_and_cascades(grounded, auth_client, database):
    run = grounded.admit()
    route = grounded.base + f"/messages/{run['message_id']}/feedback"
    assert grounded.client.put(route, json={"vote": 1}, headers=grounded.h).status_code == 404
    await grounded.execute()
    first = grounded.client.put(
        route, json={"vote": 1, "reason": "Useful <script>text</script>"}, headers=grounded.h
    )
    assert first.status_code == 200, first.text
    assert (
        grounded.client.put(
            route, json={"vote": 1, "reason": "Useful <script>text</script>"}, headers=grounded.h
        ).json()
        == first.json()
    )
    changed = grounded.client.put(route, json={"vote": -1}, headers=grounded.h)
    assert changed.status_code == 200 and changed.json()["reason"] == ""
    for body in [
        {"vote": 0},
        {"vote": True},
        {"vote": "1"},
        {"vote": 1, "reason": "x" * 1001},
        {"vote": 1, "user_id": str(uuid4())},
    ]:
        assert grounded.client.put(route, json=body, headers=grounded.h).status_code == 422
    assert grounded.client.put(route, json={"vote": 1}).status_code == 403
    client, state = auth_client
    state["subject"] = str(uuid4())
    other = sign_in(client, state)
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.memberships(workspace_id,user_id,role) VALUES(%s,%s,'admin')",
            (grounded.artifact.workspace_id, other["user"]["id"]),
        )
    assert client.put(route, json={"vote": 1}, headers=headers(other)).status_code == 404
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM app.feedback WHERE message_id=%s", (run["message_id"],)
            ).fetchone()[0]
            == 1
        )
        # Remove the run pointer in a rollback-only transaction, then verify the real FK cascade.
        conn.execute("UPDATE app.runs SET assistant_message_id=NULL WHERE id=%s", (run["id"],))
        conn.execute("DELETE FROM app.messages WHERE id=%s", (run["message_id"],))
        assert (
            conn.execute(
                "SELECT count(*) FROM app.feedback WHERE message_id=%s", (run["message_id"],)
            ).fetchone()[0]
            == 0
        )
        conn.rollback()


async def test_feedback_abstention_and_source_revocation(grounded, database):
    # A schema-valid abstention still reaches final state and can receive feedback.
    grounded.observed["answer"] = {
        "status": "abstained",
        "sentences": [],
        "limitations": ["Insufficient evidence."],
    }
    run = grounded.admit()
    await grounded.execute()
    route = grounded.base + f"/messages/{run['message_id']}/feedback"
    response = grounded.client.put(route, json={"vote": -1}, headers=grounded.h)
    assert response.status_code == 200, response.text
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.documents SET deleted_at=now() WHERE id=%s",
            (grounded.artifact.document_id,),
        )
    assert grounded.client.put(route, json={"vote": 1}, headers=grounded.h).status_code == 404


async def test_usage_reconciles_stored_ledger_utc_windows_and_live_roles(grounded, database):
    run = grounded.admit()
    await grounded.execute()
    with psycopg.connect(database["operator"]) as conn:
        workspace, user = conn.execute(
            "SELECT workspace_id,user_id FROM app.runs WHERE id=%s", (run["id"],)
        ).fetchone()
        other = uuid4()
        conn.execute(
            "INSERT INTO app.users(id,issuer,subject,email,display_name) VALUES(%s,'fixture',%s,%s,'Other')",
            (other, str(other), f"{other}@example.test"),
        )
        # Fixed UTC accounting dates intentionally differ from creation time: attribution follows reservation.
        for actor, day, state, cost, reserved, estimated, inputs, outputs in [
            (user, "2026-01-31", "settled", 1234, 2000, False, 100, 20),
            (user, "2026-02-01", "estimated", 3000, 3000, True, None, None),
            (user, "2026-02-01", "reserved", None, 5000, False, None, None),
            (other, "2026-02-01", "settled", 200, 200, False, 50, 10),
        ]:
            conn.execute(
                """INSERT INTO app.model_usage(workspace_id,id,user_id,attempt_no,purpose,model_key,resolved_model_id,price_version,
            reserved_micro_usd,cost_micro_usd,state,day_start,month_start,usage_estimated,input_tokens,output_tokens)
            VALUES(%s,%s,%s,1,'generation','historical-model','historical-model','old-price',%s,%s,%s,%s,date_trunc('month',%s::date)::date,%s,%s,%s)""",
                (
                    workspace,
                    uuid4(),
                    actor,
                    reserved,
                    cost,
                    state,
                    day,
                    day,
                    estimated,
                    inputs,
                    outputs,
                ),
            )
    url = grounded.base + "/usage"
    params = {"start_date": "2026-01-31", "end_date": "2026-02-01"}
    response = grounded.client.get(url, params=params)
    assert response.status_code == 200, response.text
    usage = response.json()
    assert usage["reserved_micro_usd"] == 5000
    assert sum(b["requests"] for b in usage["buckets"]) == 4
    assert sum(b["cost_micro_usd"] for b in usage["buckets"]) == 4434
    assert sum(b["input_tokens"] for b in usage["buckets"]) == 150
    assert sum(b["estimated_attempts"] for b in usage["buckets"]) == 1
    assert set(usage) == {"buckets", "reserved_micro_usd", "currency"}
    assert "response" not in response.text and "Fixture evidence" not in response.text
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.memberships SET role='viewer' WHERE workspace_id=%s AND user_id=%s",
            (workspace, user),
        )
    own = grounded.client.get(url, params=params).json()
    assert sum(b["requests"] for b in own["buckets"]) == 3
    assert sum(b["cost_micro_usd"] for b in own["buckets"]) == 4234
    assert grounded.client.get(
        url, params={"start_date": "2025-01-01", "end_date": "2025-01-01"}
    ).json() == {"buckets": [], "reserved_micro_usd": 0, "currency": "USD"}
    assert (
        len(
            grounded.client.get(
                url, params={"start_date": "2026-02-01", "end_date": "2026-02-01"}
            ).json()["buckets"]
        )
        == 1
    )
    for start, end in [
        ("2026-02-01", "2026-01-31"),
        ("2026-01-01", "2026-04-01"),
        ("not-a-date", "2026-01-01"),
    ]:
        assert (
            grounded.client.get(url, params={"start_date": start, "end_date": end}).status_code
            == 422
        )
    assert (
        grounded.client.get(
            url, params={"start_date": "2026-01-01", "end_date": "2026-03-31"}
        ).status_code
        == 200
    )
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "DELETE FROM app.memberships WHERE workspace_id=%s AND user_id=%s", (workspace, user)
        )
    assert grounded.client.get(url, params=params).status_code == 404

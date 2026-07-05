from uuid import uuid4

import psycopg
import pytest

from tests.support import headers, sign_in

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("condition", ["missing", "stale", "mismatch", "endpoint", "open"])
def test_unprobed_models_are_disabled_and_cannot_admit_runs(auth_client, database, condition):
    client, state = auth_client
    identity = sign_in(client, state)
    h = headers(identity)
    workspace = client.post("/v1/workspaces", json={"name": "Readiness"}, headers=h).json()["id"]
    base = f"/v1/workspaces/{workspace}"
    conversation = client.post(
        base + "/conversations", json={"title": "Readiness"}, headers=h
    ).json()["id"]
    kb = uuid4()
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.knowledge_bases(workspace_id,id,name,created_by) VALUES(%s,%s,%s,%s)",
            (workspace, kb, "Sources", identity["user"]["id"]),
        )
        if condition == "missing":
            conn.execute("DELETE FROM app.provider_health")
        elif condition == "stale":
            conn.execute("UPDATE app.provider_health SET last_probe_at=now()-interval '6 minutes'")
        elif condition == "mismatch":
            conn.execute("UPDATE app.provider_health SET capability_report='{}'")
        elif condition == "endpoint":
            conn.execute(
                "UPDATE app.provider_health SET capability_report=capability_report - 'endpoint_sha256'"
            )
        else:
            conn.execute("UPDATE app.provider_health SET state='open'")
    page = client.get(base + "/models").json()
    assert page["items"] and not any(model["available"] for model in page["items"])
    assert all(
        set(model)
        == {
            "key",
            "display_name",
            "provider",
            "privacy_class",
            "available",
            "supports_schema",
            "context_cap",
            "unavailable_reason",
        }
        for model in page["items"]
    )
    response = client.post(
        base + f"/conversations/{conversation}/runs",
        json={
            "query": "Question",
            "kb_ids": [str(kb)],
            "model_key": "auto",
            "intent": "ask",
            "expected_conversation_version": 1,
        },
        headers=h,
    )
    assert response.status_code == 409 and response.json()["code"] == "model_unavailable"
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM app.runs WHERE workspace_id=%s", (workspace,)
            ).fetchone()[0]
            == 0
        )


def test_current_probe_respects_local_only_and_bound_cursor(auth_client, database):
    client, state = auth_client
    identity = sign_in(client, state)
    h = headers(identity)
    workspaces = [
        client.post(
            "/v1/workspaces",
            json={"name": f"Ready {i}"},
            headers={**h, "Idempotency-Key": uuid4().hex},
        ).json()["id"]
        for i in range(2)
    ]
    base = f"/v1/workspaces/{workspaces[0]}"
    models = client.get(base + "/models").json()["items"]
    assert {model["key"] for model in models if model["available"]} == {
        "paid_standard",
        "local_standard",
    }
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.workspaces SET privacy_mode='local_only' WHERE id=%s", (workspaces[0],)
        )
    models = client.get(base + "/models").json()["items"]
    assert {model["key"] for model in models if model["available"]} == {"local_standard"}
    cursor = client.get(base + "/models?limit=1").json()["next_cursor"]
    assert cursor
    assert (
        client.get(f"/v1/workspaces/{workspaces[1]}/models", params={"cursor": cursor}).status_code
        == 422
    )

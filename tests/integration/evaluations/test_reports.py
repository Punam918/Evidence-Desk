import copy
from uuid import uuid4

import psycopg
import pytest

from tests.integration.tenancy.test_workspaces import headers
from tests.support import sign_in

pytestmark = pytest.mark.integration


def report():
    return {
        "dataset_version": "benchmark-v1-draft",
        "dataset_sha256": "a" * 64,
        "config_sha256": "b" * 64,
        "git_sha": None,
        "source_snapshot_sha256": "c" * 64,
        "evidence_scope": "draft_diagnostic",
        "variant": "dense",
        "model_key": "retrieval-only",
        "hardware": "CPU test fixture",
        "status": "completed",
        "case_count": 100,
        "metrics": [
            {"name": "recall_at_20", "value": 0.5, "n": 80, "ci_low": 0.4, "ci_high": 0.6},
            {"name": "ndcg_at_10", "value": None, "n": 0, "ci_low": None, "ci_high": None},
        ],
        "artifact_sha256": "d" * 64,
    }


@pytest.fixture
def setup(auth_client):
    client, state = auth_client
    session = sign_in(client, state)
    workspace = client.post(
        "/v1/workspaces", json={"name": "Evaluation fixtures"}, headers=headers(session)
    ).json()["id"]
    return client, session, workspace, f"/v1/workspaces/{workspace}/evaluations"


def test_immutable_idempotent_import_and_tenant_bound_pagination(setup, database):
    client, session, workspace, path = setup
    body, h = report(), headers(session)
    first = client.post(path, json=body, headers=h)
    assert first.status_code == 201, first.text
    uid = first.json()["id"]
    assert client.get(first.headers["Location"]).json() == first.json()
    assert client.post(path, json=body, headers=h).json() == first.json()
    assert client.post(path, json={**body, "variant": "hybrid"}, headers=h).status_code == 409
    second = client.post(path, json={**body, "variant": "hybrid"}, headers=headers(session))
    assert second.status_code == 201
    page = client.get(path, params={"limit": 1}).json()
    assert page["items"][0]["id"] == second.json()["id"]
    tail = client.get(path, params={"limit": 1, "cursor": page["next_cursor"]}).json()
    assert [x["id"] for x in tail["items"]] == [uid] and tail["next_cursor"] is None
    assert client.get(path, params={"cursor": page["next_cursor"] + "x"}).status_code == 422
    other = client.post("/v1/workspaces", json={"name": "Other"}, headers=headers(session)).json()[
        "id"
    ]
    assert client.get(f"/v1/workspaces/{other}/evaluations/{uid}").status_code == 404
    assert (
        client.get(
            f"/v1/workspaces/{other}/evaluations", params={"cursor": page["next_cursor"]}
        ).status_code
        == 422
    )
    assert client.get(f"/v1/workspaces/{uuid4()}/evaluations/{uid}").status_code == 404
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM app.audit_events WHERE workspace_id=%s AND action='evaluation.imported'",
                (workspace,),
            ).fetchone()[0]
            == 2
        )
        conn.execute("UPDATE app.memberships SET role='viewer' WHERE workspace_id=%s", (workspace,))
    assert client.get(path).status_code == 403
    assert client.get(first.headers["Location"]).status_code == 403
    assert client.post(path, json=body, headers=h).status_code == 403  # Replay reauthorizes.
    with psycopg.connect(database["operator"]) as conn:
        conn.execute("DELETE FROM app.memberships WHERE workspace_id=%s", (workspace,))
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "bad",
    [
        "unknown",
        "legacy",
        "hash",
        "negative_n",
        "empty_n",
        "null_value",
        "interval",
        "half_interval",
        "duplicate",
        "raw_label",
        "raw_hardware",
        "too_many",
    ],
)
def test_rejects_raw_content_and_invalid_metrics(setup, bad):
    client, session, _, path = setup
    body = copy.deepcopy(report())
    if bad == "unknown":
        body["prompts"] = ["private canary"]
    elif bad == "legacy":
        body["evidence_scope"] = "legacy_unclassified"
    elif bad == "hash":
        body["source_snapshot_sha256"] = None
    elif bad == "negative_n":
        body["metrics"][0]["n"] = -1
    elif bad == "empty_n":
        body["metrics"][0]["n"] = 0
    elif bad == "null_value":
        body["metrics"][0]["value"] = None
    elif bad == "interval":
        body["metrics"][0]["ci_low"] = 0.8
    elif bad == "half_interval":
        body["metrics"][0]["ci_high"] = None
    elif bad == "duplicate":
        body["metrics"].append(body["metrics"][0])
    elif bad == "raw_label":
        body["variant"] = "<script>private canary</script>"
    elif bad == "raw_hardware":
        body["hardware"] = "device\nprivate canary"
    elif bad == "too_many":
        body["metrics"] = body["metrics"] * 51
    result = client.post(path, json=body, headers=headers(session))
    assert result.status_code == 422, result.text
    assert "private canary" not in result.text
    assert client.get(path).json()["items"] == []


def test_csrf_and_legacy_unknown_provenance(setup, database):
    client, session, workspace, path = setup
    assert (
        client.post(path, json=report(), headers={"Idempotency-Key": "a" * 16}).status_code == 403
    )
    row = client.post(path, json=report(), headers=headers(session)).json()
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.evaluation_runs SET evidence_scope='legacy_unclassified',source_snapshot_sha256=NULL WHERE workspace_id=%s AND id=%s",
            (workspace, row["id"]),
        )
    legacy = client.get(path + "/" + row["id"])
    assert legacy.status_code == 200
    assert legacy.json()["source_snapshot_sha256"] is None
    assert legacy.json()["evidence_scope"] == "legacy_unclassified"

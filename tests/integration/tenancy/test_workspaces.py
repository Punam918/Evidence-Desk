import asyncio
import secrets
from uuid import uuid4

import psycopg
import pytest
from evidencedesk.db.session import AuthContext, engine
from evidencedesk.errors import Problem
from evidencedesk.tenancy.models import RoleInput
from evidencedesk.tenancy.service import TenancyService

from tests.support import sign_in

pytestmark = pytest.mark.integration


def headers(session):
    return {
        "Origin": "https://app.example.test",
        "X-CSRF-Token": session["csrf_token"],
        "Idempotency-Key": secrets.token_hex(16),
    }


def test_creation_idempotency_pagination_and_policy(auth_client):
    client, state = auth_client
    session = sign_in(client, state)
    h = headers(session)
    response = client.post("/v1/workspaces", json={"name": "Research"}, headers=h)
    assert response.status_code == 201, response.text
    workspace = response.json()
    wid = workspace["id"]
    assert client.post("/v1/workspaces", json={"name": "Research"}, headers=h).json() == workspace
    assert client.post("/v1/workspaces", json={"name": "Changed"}, headers=h).status_code == 409
    assert client.get(f"/v1/workspaces/{uuid4()}").status_code == 404
    assert client.get(f"/v1/workspaces/{wid}").json()["role"] == "owner"
    assert (
        client.post("/v1/workspaces", json={"name": "Two"}, headers=headers(session)).status_code
        == 201
    )
    page = client.get("/v1/workspaces?limit=1").json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    page2 = client.get("/v1/workspaces", params={"limit": 1, "cursor": page["next_cursor"]}).json()
    assert len(page2["items"]) == 1 and page2["items"][0]["id"] != page["items"][0]["id"]
    assert (
        client.get("/v1/workspaces", params={"cursor": page["next_cursor"] + "x"}).status_code
        == 422
    )
    policy = client.get(f"/v1/workspaces/{wid}/settings").json()
    body = {**policy, "expected_version": policy.pop("version", 1)}
    body.pop("version", None)
    body["privacy_mode"] = "local_only"
    changed = client.patch(f"/v1/workspaces/{wid}/settings", json=body, headers=h)
    assert changed.status_code == 200, changed.text
    assert changed.json()["version"] == 2
    assert client.patch(f"/v1/workspaces/{wid}/settings", json=body, headers=h).status_code == 409
    body["expected_version"] = 2
    body["daily_budget_usd"] = "1000.00"
    assert client.patch(f"/v1/workspaces/{wid}/settings", json=body, headers=h).status_code == 422


def test_role_scope_and_membership_revocation(auth_client, database):
    client, state = auth_client
    session = sign_in(client, state)
    actor = session["user"]["id"]
    h = headers(session)
    workspace = client.post("/v1/workspaces", json={"name": "Scoped"}, headers=h).json()["id"]
    members = client.get(f"/v1/workspaces/{workspace}/members")
    assert members.status_code == 200, members.text
    assert members.json()["items"][0]["user_id"] == actor
    assert (
        client.delete(f"/v1/workspaces/{workspace}/members/{actor}", headers=h).status_code == 409
    )
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.memberships SET role='viewer' WHERE workspace_id=%s AND user_id=%s",
            (workspace, actor),
        )
    assert (
        client.post(
            f"/v1/workspaces/{workspace}/members",
            json={"email": "other@example.test", "role": "owner"},
            headers=h,
        ).status_code
        == 403
    )
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "DELETE FROM app.memberships WHERE workspace_id=%s AND user_id=%s", (workspace, actor)
        )
    assert client.get(f"/v1/workspaces/{workspace}").status_code == 404
    assert client.get("/v1/workspaces").json()["items"] == []


async def test_concurrent_last_owner_demotions(database, tmp_path):
    # Independent DB connections race real locks; authorization is never stubbed.
    from evidencedesk.config import Settings

    for name in ["session", "cursor", "client"]:
        (tmp_path / name).write_text(secrets.token_hex(32))
    settings = Settings(
        database_url=database["ed_app"],
        identity_database_url=database["ed_identity"],
        oidc_issuer="https://id.example.test",
        session_key_file=tmp_path / "session",
        cursor_key_file=tmp_path / "cursor",
        oidc_client_secret_file=tmp_path / "client",
    )
    workspace, first, second = uuid4(), uuid4(), uuid4()
    with psycopg.connect(database["operator"]) as conn:
        for uid in [first, second]:
            conn.execute(
                "INSERT INTO app.users(id,issuer,subject,email,display_name) VALUES(%s,%s,%s,%s,%s)",
                (uid, "test", str(uid), f"{uid}@test.example", "Owner"),
            )
        conn.execute("INSERT INTO app.workspaces(id,name) VALUES(%s,%s)", (workspace, "Race"))
        for uid in [first, second]:
            conn.execute(
                "INSERT INTO app.memberships(workspace_id,user_id,role) VALUES(%s,%s,'owner')",
                (workspace, uid),
            )
    db, identity_db = engine(database["ed_app"]), engine(database["ed_identity"])
    service = TenancyService(settings, db, identity_db)
    try:
        results = await asyncio.gather(
            *[
                service.change_member(
                    AuthContext(uid, workspace, "owner", 1, uuid4()),
                    uid,
                    RoleInput(role="viewer", expected_version=1),
                )
                for uid in [first, second]
            ],
            return_exceptions=True,
        )
        assert sum(isinstance(r, Problem) and r.code == "last_owner" for r in results) == 1, results
        with psycopg.connect(database["operator"]) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM app.memberships WHERE workspace_id=%s AND role='owner'",
                    (workspace,),
                ).fetchone()[0]
                == 1
            )
    finally:
        await db.dispose()
        await identity_db.dispose()


def test_workspace_delete_commits_tombstone_before_acknowledgement(auth_client, database):
    client, state = auth_client
    session = sign_in(client, state)
    h = headers(session)
    workspace = client.post("/v1/workspaces", json={"name": "Delete"}, headers=h).json()["id"]
    response = client.delete(f"/v1/workspaces/{workspace}", headers=h)
    assert response.status_code == 202, response.text
    assert client.get(f"/v1/workspaces/{workspace}").status_code == 404
    assert client.get(f"/v1/workspaces/{workspace}/members").status_code == 404
    assert client.get("/v1/workspaces").json()["items"] == []
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM app.deletion_tombstones WHERE workspace_id=%s AND entity_type=%s",
                (workspace, "workspace"),
            ).fetchone()[0]
            == 1
        )
        assert conn.execute(
            "SELECT kind,status FROM app.jobs WHERE workspace_id=%s AND id=%s",
            (workspace, response.json()["job_id"]),
        ).fetchone() == ("purge", "queued")


def test_unpromoted_memory_features_cannot_be_enabled_by_environment_flag(auth_client, monkeypatch):
    client, state = auth_client
    session = sign_in(client, state)
    h = headers(session)
    workspace = client.post("/v1/workspaces", json={"name": "Promotion gate"}, headers=h).json()[
        "id"
    ]
    monkeypatch.setattr(client.app.state.settings, "rewrite_enabled", True)
    policy = client.get(f"/v1/workspaces/{workspace}/settings").json()
    version = policy.pop("version")
    for override in ({"retrieval_profile": "hybrid_rewrite"}, {"summaries_enabled": True}):
        response = client.patch(
            f"/v1/workspaces/{workspace}/settings",
            json={**policy, **override, "expected_version": version},
            headers=h,
        )
        assert response.status_code == 422 and response.json()["code"] == "feature_unavailable"

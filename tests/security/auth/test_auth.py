import secrets

import psycopg
import pytest

from tests.support import begin, sign_in

pytestmark = pytest.mark.integration


def test_login_csrf_logout_and_replay(auth_client):
    client, state = auth_client
    callback = begin(client, state, "/workspaces/example")
    response = client.get("/auth/callback", params=callback)
    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/workspaces/example"
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    me = client.get("/v1/me")
    assert me.status_code == 200
    assert set(me.json()) == {"user", "csrf_token", "expires_at"}
    assert client.get("/auth/callback", params=callback).status_code == 401
    headers = {
        "Origin": "https://app.example.test",
        "X-CSRF-Token": me.json()["csrf_token"],
        "Idempotency-Key": secrets.token_hex(16),
    }
    assert (
        client.post(
            "/auth/logout", headers={**headers, "Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        client.post("/auth/logout", headers={**headers, "X-CSRF-Token": "invalid"}).status_code
        == 403
    )
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/v1/me").status_code == 401
    assert state["calls"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "https://evil.example"},
        {"iss": ["https://id.example.test"]},
        {"aud": "foreign"},
        {"exp": 0},
        {"iat": 99999999999},
        {"nonce": "wrong"},
        {"email_verified": False},
        {"sub": ""},
        {"aud": ["ed-test", "foreign"]},
        {"azp": "foreign"},
    ],
)
def test_invalid_oidc_claims_never_create_session(auth_client, database, change):
    client, state = auth_client
    state["changes"] = change
    response = client.get("/auth/callback", params=begin(client, state))
    assert response.status_code == 401, response.text
    assert "ed_session" not in client.cookies
    with psycopg.connect(database["operator"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM app.users WHERE subject=%s", (state["subject"],)
            ).fetchone()[0]
            == 0
        )


def test_browser_binding_and_one_use_state(auth_client):
    client, state = auth_client
    callback = begin(client, state)
    client.cookies.clear()
    assert client.get("/auth/callback", params=callback).status_code == 401
    client.cookies.set("ed_login", "wrong", path="/auth")
    assert client.get("/auth/callback", params=callback).status_code == 401
    assert state["calls"] == 0


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example",
        "https://evil.example",
        "/workspaces/../x",
        "/workspaces/%2e%2e",
        "/workspaces/\\evil",
        "/workspaces/a?next=https://evil",
    ],
)
def test_redirect_allowlist(auth_client, path):
    client, _ = auth_client
    assert client.get("/auth/login", params={"return_to": path}).status_code == 422


@pytest.mark.parametrize("expiration", ["idle", "absolute", "revoked"])
def test_server_session_expiration(auth_client, database, expiration):
    client, state = auth_client
    sign_in(client, state)
    sql = {
        "idle": "last_seen_at=now()-interval '9 hours'",
        "absolute": "expires_at=now()-interval '1 second'",
        "revoked": "revoked_at=now()",
    }[expiration]
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "UPDATE app.sessions SET "
            + sql
            + " WHERE user_id=(SELECT id FROM app.users WHERE subject=%s)",
            (state["subject"],),
        )
    assert client.get("/v1/me").status_code == 401

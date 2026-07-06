from urllib.parse import parse_qs, urlsplit


def begin(client, state, path="/"):
    response = client.get("/auth/login", params={"return_to": path})
    assert response.status_code == 302
    params = parse_qs(urlsplit(response.headers["location"]).query)
    state["nonce"] = params["nonce"][0]
    state["challenge"] = params["code_challenge"][0]
    assert params["code_challenge_method"] == ["S256"]
    return {"code": "one-use-code", "state": params["state"][0]}


def sign_in(client, state):
    response = client.get("/auth/callback", params=begin(client, state))
    assert response.status_code == 302, response.text
    return client.get("/v1/me").json()


def headers(session):
    import secrets

    return {
        "Origin": "https://app.example.test",
        "X-CSRF-Token": session["csrf_token"],
        "Idempotency-Key": secrets.token_hex(16),
    }

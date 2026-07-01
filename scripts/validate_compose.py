"""Real local HTTPS/OIDC/API smoke. Never prints credentials or records auth responses."""

import argparse
import hashlib
import io
import json
import re
import socket
import ssl
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://evidencedesk.localhost:8443"


def mounted_file(project: str, name: str, limit: int = 8192) -> bytes:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,60}", project):
        raise ValueError("Invalid local Compose project")
    if name not in ("operator/demo_password", "trust/ca.crt"):
        raise ValueError("Unrecognized smoke-test input")
    raw = subprocess.check_output(
        ["docker", "cp", f"{project}-bootstrap-1:/secrets/{name}", "-"],
        stderr=subprocess.DEVNULL,
    )
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        members = archive.getmembers()
        if len(members) != 1 or not members[0].isfile() or members[0].size > limit:
            raise ValueError("Unexpected bootstrap artifact")
        stream = archive.extractfile(members[0])
        assert stream is not None
        return stream.read(limit)


def require(response: httpx.Response, status: int) -> httpx.Response:
    if response.status_code != status:
        # Endpoint path/status are sufficient; query parameters/body may contain a code/token.
        raise ValueError(f"Unexpected HTTP status {response.status_code} for {response.url.path}")
    return response


def login(client: httpx.Client, password: str) -> dict:
    login_response = require(client.get("/auth/login"), 302)
    authorization = login_response.headers["location"]
    if not authorization.startswith(ORIGIN + "/identity/realms/evidencedesk/"):
        raise ValueError("Authorization escaped configured realm")
    form_page = require(client.get(authorization), 200)
    form = BeautifulSoup(form_page.text, "html.parser").find("form", id="kc-form-login")
    if form is None or not isinstance(form.get("action"), str):
        raise ValueError("Identity provider did not present its login form")
    action = str(form["action"])
    if not action.startswith(ORIGIN + "/identity/realms/evidencedesk/"):
        raise ValueError("Identity form escaped configured realm")
    authenticated = require(
        client.post(action, data={"username": "demo", "password": password}), 302
    )
    callback = authenticated.headers["location"]
    if not callback.startswith(ORIGIN + "/auth/callback?"):
        raise ValueError("Identity callback escaped exact registered URI")
    require(client.get(callback), 302)
    return require(client.get("/v1/me"), 200).json()


def run(project: str, previous: dict | None = None) -> dict:
    ca = mounted_file(project, "trust/ca.crt")
    password = mounted_file(project, "operator/demo_password").decode().strip()
    context = ssl.create_default_context(cadata=ca.decode())
    resolver = socket.getaddrinfo

    def local_dns(host, *args, **kwargs):
        return resolver("127.0.0.1" if host == "evidencedesk.localhost" else host, *args, **kwargs)

    with (
        patch("socket.getaddrinfo", local_dns),
        httpx.Client(
            base_url=ORIGIN, verify=context, trust_env=False, timeout=15, follow_redirects=False
        ) as client,
    ):
        require(client.get("/"), 200)
        require(client.get("/health/ready"), 200)
        require(client.get("/v1/me"), 401)
        require(client.get("/identity/admin/"), 404)
        issuer = ORIGIN + "/identity/realms/evidencedesk"
        discovery = require(client.get(issuer + "/.well-known/openid-configuration"), 200).json()
        assert discovery["issuer"] == issuer
        identity = login(client, password)
        mutation = {
            "origin": ORIGIN,
            "x-csrf-token": identity["csrf_token"],
            "idempotency-key": str(uuid4()),
        }
        if previous:
            if previous["ca_sha256"] != hashlib.sha256(ca).hexdigest():
                raise ValueError("Retained-volume restart rotated the trust root")
            workspace_id = previous["workspace_id"]
            workspace = require(client.get("/v1/workspaces/" + workspace_id), 200).json()
            assert workspace["name"] == "Compose verification"
        else:
            workspace = require(
                client.post(
                    "/v1/workspaces", headers=mutation, json={"name": "Compose verification"}
                ),
                201,
            ).json()
            workspace_id = workspace["id"]
        # A valid cookie without the request-bound CSRF proof must not mutate state.
        require(
            client.post(
                "/v1/workspaces",
                headers={"origin": ORIGIN, "idempotency-key": str(uuid4())},
                json={"name": "must not exist"},
            ),
            403,
        )
        require(client.get("/v1/workspaces/" + workspace_id), 200)
        require(
            client.post("/auth/logout", headers={**mutation, "idempotency-key": str(uuid4())}), 204
        )
        require(client.get("/v1/me"), 401)
    return {
        "version": 1,
        "collected_at": datetime.now(UTC).isoformat(),
        "project": project,
        "origin": ORIGIN,
        "ca_sha256": hashlib.sha256(ca).hexdigest(),
        "workspace_id": workspace_id,
        "retained_restart": previous is not None,
        "checks": {
            name: "passed"
            for name in (
                "verified_https",
                "same_origin_discovery",
                "native_oidc_pkce_login",
                "authenticated_workspace",
                "csrf_required",
                "admin_route_private",
                "logout_revocation",
            )
        },
        "limitations": [
            "HTTP protocol smoke; no browser rendering, ingestion or generation quality claim"
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="evidencedesk")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new report path; evidence is not overwritten")
    previous = json.loads(args.previous.read_text()) if args.previous else None
    try:
        report = run(args.project, previous)
    except Exception as exc:
        # Exceptions from HTTP clients may include authorization query parameters.
        print(json.dumps({"status": "failed", "type": type(exc).__name__}))
        raise SystemExit(1) from None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": "passed", "report": str(args.output)}))


if __name__ == "__main__":
    main()

import hashlib
import json
import os
import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import psycopg
import pytest
import yaml
from evidencedesk.app import create_app
from evidencedesk.config import Settings
from evidencedesk.models.catalog import endpoint_sha256
from evidencedesk.models.registry import load_registry
from fastapi.testclient import TestClient
from joserfc import jwk, jwt
from psycopg import sql

"""Real, disposable infrastructure: never point destructive tests at an external database."""


ROOT = Path(__file__).resolve().parents[1]
PG_IMAGE = "postgres@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336"


@pytest.fixture(scope="session")
def database():
    name = "ed-test-" + uuid4().hex[:12]
    password = secrets.token_hex(24)
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "--memory",
            "1g",
            "--tmpfs",
            "/var/lib/postgresql",
            "-p",
            "127.0.0.1::5432",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            PG_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    try:
        port = (
            subprocess.check_output(["docker", "port", name, "5432/tcp"], text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        dsn = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
        for _ in range(100):
            try:
                with psycopg.connect(dsn):
                    break
            except psycopg.OperationalError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Disposable PostgreSQL did not start")
        env = {**os.environ, "ED_MIGRATION_DATABASE_URL": dsn}
        subprocess.run(
            [str(ROOT / ".venv/bin/alembic"), "upgrade", "head"], cwd=ROOT, env=env, check=True
        )
        subprocess.run(
            [str(ROOT / ".venv/bin/python"), "scripts/setup_checkpoints.py"],
            cwd=ROOT,
            env=env,
            check=True,
        )
        with psycopg.connect(dsn, autocommit=True) as conn:
            for role in ["ed_app", "ed_identity", "ed_dispatch", "ed_checkpoint", "ed_monitor"]:
                conn.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
        yield {
            "operator": dsn,
            **{
                role: dsn.replace("postgres:", role + ":", 1)
                for role in ["ed_app", "ed_identity", "ed_dispatch", "ed_checkpoint", "ed_monitor"]
            },
        }
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


pytestmark = pytest.mark.integration


@pytest.fixture
def auth_client(database, tmp_path):
    for key in ["session", "cursor", "client"]:
        (tmp_path / key).write_text(secrets.token_hex(32))
    registry = yaml.safe_load((ROOT / "contracts/config/models.yaml").read_text())
    for model in registry["models"]:
        if model["key"] == "local_standard":
            model["resolved_digest"] = "a" * 64
    registry_path = tmp_path / "models.yaml"
    registry_path.write_text(yaml.safe_dump(registry))
    settings = Settings(
        model_config_path=registry_path,
        openai_key_file=tmp_path / "client",
        local_model_url="http://127.0.0.1:11435",
        database_url=database["ed_app"],
        identity_database_url=database["ed_identity"],
        oidc_issuer="https://id.example.test",
        oidc_client_id="ed-test",
        public_origin="https://app.example.test",
        session_key_file=tmp_path / "session",
        cursor_key_file=tmp_path / "cursor",
        oidc_client_secret_file=tmp_path / "client",
    )
    key = jwk.RSAKey.generate_key(2048, parameters={"kid": "test"})
    state = {"changes": {}, "nonce": "", "challenge": "", "subject": str(uuid4()), "calls": 0}

    def provider(request):
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": settings.oidc_issuer,
                    "authorization_endpoint": settings.oidc_issuer + "/authorize",
                    "token_endpoint": settings.oidc_issuer + "/token",
                    "jwks_uri": settings.oidc_issuer + "/keys",
                },
            )
        if path == "/keys":
            return httpx.Response(200, json={"keys": [key.as_dict(private=False)]})
        if path == "/token":
            import base64

            fields = parse_qs(request.content.decode())
            assert fields["grant_type"] == ["authorization_code"]
            assert fields["redirect_uri"] == [settings.public_origin + "/auth/callback"]
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(fields["code_verifier"][0].encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            assert challenge == state["challenge"]
            state["calls"] += 1
            claims = {
                "iss": settings.oidc_issuer,
                "sub": state["subject"],
                "aud": settings.oidc_client_id,
                "exp": int(time.time()) + 120,
                "iat": int(time.time()),
                "nonce": state["nonce"],
                "email": state["subject"] + "@example.test",
                "email_verified": True,
                "name": "Test Member",
            }
            claims.update(state["changes"])
            return httpx.Response(
                200, json={"id_token": jwt.encode({"alg": "RS256", "kid": "test"}, claims, key)}
            )
        raise AssertionError("Unexpected outbound request")

    with psycopg.connect(database["operator"]) as conn:
        prior_workspaces = [row[0] for row in conn.execute("SELECT id FROM app.workspaces")]
        for model in load_registry(registry_path):
            conn.execute(
                "INSERT INTO app.provider_health(model_key,state,window_start,last_probe_at,capability_report) VALUES(%s,'closed',now(),now(),%s::jsonb) ON CONFLICT(model_key) DO UPDATE SET state='closed',last_probe_at=now(),capability_report=EXCLUDED.capability_report",
                (
                    model.key,
                    json.dumps(
                        {
                            "passed": True,
                            "config_sha256": model.fingerprint,
                            "endpoint_sha256": endpoint_sha256(model, settings),
                            "scope": "scripted_test_fixture",
                        }
                    ),
                ),
            )
    with TestClient(
        create_app(settings, httpx.MockTransport(provider)),
        base_url=settings.public_origin,
        follow_redirects=False,
    ) as client:
        try:
            yield client, state
        finally:
            with psycopg.connect(database["operator"]) as conn:
                conn.execute(
                    "DELETE FROM app.workspaces WHERE NOT (id=ANY(%s))", (prior_workspaces,)
                )


@pytest.fixture
def object_store(auth_client):
    import logging

    from evidencedesk.storage.s3 import S3Store
    from moto.server import ThreadedMotoServer

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    store = S3Store(
        f"http://{host}:{port}", "us-east-1", "ed-test-" + uuid4().hex, "testing", "testing"
    )
    store.client.create_bucket(Bucket=store.bucket)
    client, _ = auth_client
    client.app.state.uploads.store = store
    try:
        yield store
    finally:
        store.close()
        server.stop()

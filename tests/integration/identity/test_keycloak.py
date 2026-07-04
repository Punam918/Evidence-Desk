"""Real HTTPS OIDC authorization-code flow against a disposable Keycloak realm."""

import ipaddress
import json
import secrets
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from bs4 import BeautifulSoup
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from evidencedesk.app import create_app
from evidencedesk.config import Settings
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_real_keycloak_login_logout(database, tmp_path):
    import ssl

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    (tmp_path / "tls.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "tls.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    password, client_secret = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    for file, secret in [
        ("session", secrets.token_hex(32)),
        ("cursor", secrets.token_hex(32)),
        ("client", client_secret),
    ]:
        (tmp_path / file).write_text(secret)
    realm = {
        "realm": "evidencedesk",
        "enabled": True,
        "sslRequired": "all",
        "registrationAllowed": False,
        "clients": [
            {
                "clientId": "evidencedesk",
                "enabled": True,
                "publicClient": False,
                "secret": client_secret,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False,
                "redirectUris": ["https://app.example.test/auth/callback"],
                "attributes": {"pkce.code.challenge.method": "S256"},
            }
        ],
        "users": [
            {
                "username": "demo",
                "enabled": True,
                "email": "demo@example.test",
                "emailVerified": True,
                "firstName": "Demo",
                "lastName": "Member",
                "credentials": [{"type": "password", "value": password, "temporary": False}],
            }
        ],
    }
    (tmp_path / "realm.json").write_text(json.dumps(realm))
    # Docker's nonroot process needs directory traversal and read access to these ephemeral fixtures.
    mount = tmp_path / "mount"
    mount.mkdir(mode=0o755)
    for filename in ["tls.crt", "tls.key", "realm.json"]:
        (mount / filename).write_bytes((tmp_path / filename).read_bytes())
        (mount / filename).chmod(0o644)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"https://localhost:{port}"
    container = "ed-idp-" + uuid4().hex[:12]
    image = json.loads(open("infra/images.lock").read())["keycloak"]
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            container,
            "--memory",
            "1g",
            "-p",
            f"127.0.0.1:{port}:8443",
            "-v",
            f"{mount}:/fixtures:ro",
            "-v",
            f"{mount}/realm.json:/opt/keycloak/data/import/realm.json:ro",
            image,
            "start-dev",
            "--import-realm",
            "--http-enabled=false",
            f"--hostname={origin}",
            "--https-certificate-file=/fixtures/tls.crt",
            "--https-certificate-key-file=/fixtures/tls.key",
        ],
        check=True,
        capture_output=True,
    )
    try:
        context = ssl.create_default_context(cafile=str(tmp_path / "tls.crt"))
        with httpx.Client(verify=context, timeout=5, follow_redirects=False) as browser:
            for _ in range(240):
                try:
                    if (
                        browser.get(
                            origin + "/realms/evidencedesk/.well-known/openid-configuration"
                        ).status_code
                        == 200
                    ):
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
            else:
                raise AssertionError(
                    "Keycloak did not become ready; inspect disposable container logs"
                )
            settings = Settings(
                database_url=database["ed_app"],
                identity_database_url=database["ed_identity"],
                public_origin="https://app.example.test",
                oidc_issuer=origin + "/realms/evidencedesk",
                session_key_file=tmp_path / "session",
                cursor_key_file=tmp_path / "cursor",
                oidc_client_secret_file=tmp_path / "client",
                tls_ca_file=tmp_path / "tls.crt",
            )
            with TestClient(
                create_app(settings), base_url=settings.public_origin, follow_redirects=False
            ) as app:
                login = app.get("/auth/login")
                form = browser.get(login.headers["location"])
                action = BeautifulSoup(form.text, "html.parser").find("form")["action"]
                result = browser.post(
                    action, data={"username": "demo", "password": password, "credentialId": ""}
                )
                assert result.status_code == 302, result.status_code
                redirect = urlsplit(result.headers["location"])
                callback = app.get(redirect.path + "?" + redirect.query)
                assert callback.status_code == 302, callback.text
                me = app.get("/v1/me")
                assert me.status_code == 200 and me.json()["user"]["email"] == "demo@example.test"
                logout = app.post(
                    "/auth/logout",
                    headers={
                        "Origin": settings.public_origin,
                        "X-CSRF-Token": me.json()["csrf_token"],
                        "Idempotency-Key": secrets.token_hex(16),
                    },
                )
                assert logout.status_code == 204
                assert app.get("/v1/me").status_code == 401
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)

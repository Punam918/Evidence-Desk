"""Compose's generated identity, trust and storage boundaries remain reproducible."""

import hashlib
import io
import json
import os
import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import boto3
import pytest
import yaml
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

from infra.bootstrap import identity, secrets, storage, tls

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def private_root(tmp_path, monkeypatch):
    secrets.initialize(tmp_path, uid=os.getuid(), gid=os.getgid(), profile_owners={})
    writer = partial(secrets.write_private, uid=os.getuid(), gid=os.getgid())
    for module in (identity, storage, tls):
        monkeypatch.setattr(module, "write_private", writer)
    return tmp_path


def fingerprints(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_identity_stable_confidential_pkce_client_and_exact_https_callback(private_root):
    identity.configure(private_root)
    first = fingerprints(private_root / "identity")
    identity.configure(private_root)
    assert fingerprints(private_root / "identity") == first
    realm = json.loads((private_root / "identity/evidencedesk-realm.json").read_text())
    assert realm["sslRequired"] == "all"
    assert realm["registrationAllowed"] is False
    assert realm["bruteForceProtected"] is True
    client = realm["clients"][0]
    assert client["publicClient"] is False
    assert client["standardFlowEnabled"] is True
    assert client["implicitFlowEnabled"] is False
    assert client["directAccessGrantsEnabled"] is False
    assert client["serviceAccountsEnabled"] is False
    assert client["redirectUris"] == [identity.PUBLIC_ORIGIN + "/auth/callback"]
    assert client["webOrigins"] == [identity.PUBLIC_ORIGIN]
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    config = (private_root / "identity/keycloak.conf").read_text()
    for setting in (
        "db-username=ed_keycloak",
        "hostname=" + identity.PUBLIC_ORIGIN + "/identity",
        "http-relative-path=/identity",
        "proxy-headers=xforwarded",
    ):
        present = setting in config.splitlines()
        assert present
    with pytest.raises(ValueError, match="fixed HTTPS origin"):
        identity.configure(private_root, "http://different.example")


def test_persistent_ca_and_https_leaf_are_exact_host_scoped_and_stable(private_root):
    tls.configure(private_root)
    before = fingerprints(private_root)
    tls.configure(private_root)
    assert fingerprints(private_root) == before
    ca = x509.load_pem_x509_certificate((private_root / "ca/ca.crt").read_bytes())
    leaf = x509.load_pem_x509_certificate((private_root / "web/tls.crt").read_bytes())
    ca.verify_directly_issued_by(ca)
    leaf.verify_directly_issued_by(ca)
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName) == [tls.HOST]
    assert (
        ExtendedKeyUsageOID.SERVER_AUTH
        in leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    )


def test_generated_certificate_passes_real_default_strict_tls_handshake(private_root):
    tls.configure(private_root)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(private_root / "web/tls.crt", private_root / "web/tls.key")
    client_context = ssl.create_default_context(cafile=str(private_root / "trust/ca.crt"))
    assert client_context.verify_flags & ssl.VERIFY_X509_STRICT
    server, client = socket.socketpair()
    errors = []

    def accept():
        try:
            with server_context.wrap_socket(server, server_side=True) as stream:
                assert stream.recv(1) == b"?"
                stream.sendall(b"!")
        except Exception as exc:
            errors.append(type(exc).__name__)

    thread = threading.Thread(target=accept, daemon=True)
    server.settimeout(5)
    client.settimeout(5)
    thread.start()
    try:
        with client_context.wrap_socket(client, server_hostname=tls.HOST) as stream:
            stream.sendall(b"?")
            assert stream.recv(1) == b"!"
    finally:
        server.close()
        client.close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
    assert (
        hashlib.sha256((private_root / "trust/ca.crt").read_bytes()).digest()
        == hashlib.sha256((private_root / "ca/ca.crt").read_bytes()).digest()
    )


@pytest.mark.parametrize("missing", ["ca/ca.key", "ca/ca.crt", "web/tls.key", "web/tls.crt"])
def test_partial_trust_material_fails_without_silent_rotation(private_root, missing):
    tls.configure(private_root)
    (private_root / missing).unlink()
    before = fingerprints(private_root)
    with pytest.raises(ValueError, match="Incomplete"):
        tls.configure(private_root)
    assert fingerprints(private_root) == before


def test_expiring_leaf_renews_without_rotating_ca(private_root):
    tls.configure(private_root)
    ca_path = private_root / "ca/ca.crt"
    ca_before = hashlib.sha256(ca_path.read_bytes()).hexdigest()
    ca = x509.load_pem_x509_certificate(ca_path.read_bytes())
    ca_key = serialization.load_pem_private_key((private_root / "ca/ca.key").read_bytes(), None)
    leaf_path = private_root / "web/tls.crt"
    leaf = x509.load_pem_x509_certificate(leaf_path.read_bytes())
    builder = (
        x509.CertificateBuilder()
        .subject_name(leaf.subject)
        .issuer_name(ca.subject)
        .public_key(leaf.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=60))
        .not_valid_after(datetime.now(UTC) + timedelta(days=10))
    )
    for extension in leaf.extensions:
        builder = builder.add_extension(extension.value, extension.critical)
    leaf_path.write_bytes(
        builder.sign(ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
    )
    tls.configure(private_root)
    renewed = x509.load_pem_x509_certificate(leaf_path.read_bytes())
    renewed.verify_directly_issued_by(ca)
    assert renewed.not_valid_after_utc > datetime.now(UTC) + timedelta(days=300)
    assert hashlib.sha256(ca_path.read_bytes()).hexdigest() == ca_before


def test_storage_credentials_are_bucket_scoped_and_no_anonymous_identity(private_root):
    storage.configure(private_root)
    first = fingerprints(private_root / "storage")
    storage.configure(private_root)
    assert fingerprints(private_root / "storage") == first
    configuration = json.loads((private_root / "storage/s3.json").read_text())
    identities = {entry["name"]: entry for entry in configuration["identities"]}
    assert set(identities) == {"bootstrap", "application"}
    assert identities["application"]["actions"] == [
        "Read:evidencedesk",
        "Write:evidencedesk",
        "List:evidencedesk",
    ]
    for entry in identities.values():
        assert len(entry["credentials"]) == 1
        complete = bool(
            entry["credentials"][0]["accessKey"] and entry["credentials"][0]["secretKey"]
        )
        assert complete
    qdrant = json.loads((private_root / "qdrant/local.yaml").read_text())
    assert qdrant["service"]["enable_cors"] is False
    separate = qdrant["service"]["api_key"] != qdrant["service"]["read_only_api_key"]
    assert separate


def stub_clients(monkeypatch):
    admin = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:1",
        aws_access_key_id="unit",
        aws_secret_access_key="unit",
    )
    anonymous = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:1",
        aws_access_key_id="unit",
        aws_secret_access_key="unit",
    )
    monkeypatch.setattr(storage, "client", lambda *_: admin)
    monkeypatch.setattr(storage.boto3, "client", lambda *_args, **_kwargs: anonymous)
    return Stubber(admin), Stubber(anonymous)


def cleanup_responses(admin):
    for _ in range(2):
        admin.add_response("delete_object", {}, {"Bucket": storage.BUCKET, "Key": ANY})


@pytest.mark.parametrize("failure", [None, "conditional_write", "anonymous_write", "versioned"])
def test_storage_capability_checks_fail_closed_and_cleanup(private_root, monkeypatch, failure):
    admin, anonymous = stub_clients(monkeypatch)
    admin.add_response("head_bucket", {}, {"Bucket": storage.BUCKET})
    admin.add_response(
        "get_bucket_versioning",
        {"Status": "Enabled"} if failure == "versioned" else {},
        {"Bucket": storage.BUCKET},
    )
    if failure != "versioned":
        expected_put = {"Bucket": storage.BUCKET, "Key": ANY, "Body": ANY, "IfNoneMatch": "*"}
        admin.add_response("put_object", {}, expected_put)
        if failure == "conditional_write":
            admin.add_response("put_object", {}, expected_put)
        else:
            admin.add_client_error(
                "put_object",
                service_error_code="PreconditionFailed",
                http_status_code=412,
                expected_params=expected_put,
            )
            payload = b"EvidenceDesk private-storage capability check\n"
            admin.add_response(
                "get_object",
                {"Body": StreamingBody(io.BytesIO(payload), len(payload))},
                {"Bucket": storage.BUCKET, "Key": ANY},
            )
            for operation, params in (
                ("list_objects_v2", {"Bucket": storage.BUCKET, "MaxKeys": 1}),
                ("get_object", {"Bucket": storage.BUCKET, "Key": ANY}),
                ("put_object", {"Bucket": storage.BUCKET, "Key": ANY, "Body": b"denied"}),
                ("delete_object", {"Bucket": storage.BUCKET, "Key": ANY}),
            ):
                if operation == "put_object" and failure == "anonymous_write":
                    anonymous.add_response(operation, {}, params)
                    break
                anonymous.add_client_error(
                    operation,
                    service_error_code="AccessDenied",
                    http_status_code=403,
                    expected_params=params,
                )
    cleanup_responses(admin)
    with admin, anonymous:
        if failure is None:
            storage.initialize(private_root)
        else:
            with pytest.raises(ValueError):
                storage.initialize(private_root)
        admin.assert_no_pending_responses()
        anonymous.assert_no_pending_responses()


class ComposeLoader(yaml.SafeLoader):
    pass


ComposeLoader.add_constructor("!reset", lambda loader, node: loader.construct_sequence(node))


def test_compose_isolates_private_services_credentials_and_parser():
    document = yaml.load((ROOT / "compose.yaml").read_text(), Loader=ComposeLoader)
    services = document["services"]
    published = {name for name, service in services.items() if service.get("ports")}
    assert published == {"web"}
    assert services["web"]["ports"] == ["127.0.0.1:8443:8443"]
    assert document["networks"]["backend"]["internal"] is True
    assert services["bootstrap"]["network_mode"] == "none"
    assert services["parser"]["network_mode"] == "none"
    assert (
        services["api"]["environment"]["ED_OIDC_ISSUER"]
        == identity.PUBLIC_ORIGIN + "/identity/realms/evidencedesk"
    )
    assert tls.HOST in services["web"]["networks"]["backend"]["aliases"]
    assert services["api"]["environment"]["ED_TLS_CA_FILE"] == "/run/trust/ca.crt"
    for name, service in services.items():
        assert service.get("privileged", False) is False
        for mount in service.get("volumes", []):
            rendered = str(mount)
            assert "docker.sock" not in rendered
            if isinstance(mount, dict) and mount.get("source") == "secrets":
                assert mount["read_only"] is True
                assert mount["volume"]["subpath"]
                assert mount["volume"]["subpath"] != "operator"
            elif isinstance(mount, str) and mount.startswith("secrets:"):
                assert name == "bootstrap"
        if name in ("api", "chat", "ingest", "index", "brief", "conversation-purge"):
            assert service["read_only"] is True
            assert service["user"] == "10001:10001"
            assert service["cap_drop"] == ["ALL"]
        for variable in service.get("environment", {}):
            assert not variable.endswith("_DATABASE_URL")
            assert variable not in ("POSTGRES_PASSWORD", "KC_DB_PASSWORD")

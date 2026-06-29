"""Persistent local CA and HTTPS leaf; trust is exported, never installed on the host."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from infra.bootstrap.secrets import read_private, write_private

HOST = "evidencedesk.localhost"


def key_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def configure(root: Path) -> None:
    ca_dir, web, public = root / "ca", root / "web", root / "trust"
    for directory in (ca_dir, web, public):
        directory.mkdir(mode=0o700, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(directory, 10001, 10001)
    now = datetime.now(UTC)
    ca_key_path, ca_cert_path = ca_dir / "ca.key", ca_dir / "ca.crt"
    if ca_key_path.exists() != ca_cert_path.exists():
        raise ValueError("Incomplete local CA; refusing silent trust rotation")
    if not ca_key_path.exists():
        ca_key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EvidenceDesk Local CA")])
        ca = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )
        write_private(ca_key_path, key_bytes(ca_key))
        write_private(ca_cert_path, ca.public_bytes(serialization.Encoding.PEM))
    else:
        loaded_ca_key = serialization.load_pem_private_key(read_private(ca_key_path), password=None)
        ca = x509.load_pem_x509_certificate(read_private(ca_cert_path))
        if not isinstance(loaded_ca_key, ec.EllipticCurvePrivateKey):
            raise ValueError("Unexpected local CA key")
        ca_key = loaded_ca_key
        ca_public = ca.public_key()
        if not isinstance(ca_public, ec.EllipticCurvePublicKey):
            raise ValueError("Unexpected local CA public key")
        if ca_key.public_key().public_numbers() != ca_public.public_numbers():
            raise ValueError("Local CA key does not match certificate")
        ca.verify_directly_issued_by(ca)
        if not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError("Local CA certificate cannot issue certificates")
        if not ca.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign:
            raise ValueError("Local CA key cannot sign certificates")
        ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
        ca.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        if ca.not_valid_before_utc > now:
            raise ValueError("Local CA is not yet valid")
        if ca.not_valid_after_utc <= now + timedelta(days=365):
            raise ValueError("Local CA needs an explicit operator renewal")
    write_private(public / "ca.crt", ca.public_bytes(serialization.Encoding.PEM))
    leaf_key_path, leaf_cert_path = web / "tls.key", web / "tls.crt"
    if leaf_key_path.exists() != leaf_cert_path.exists():
        raise ValueError("Incomplete local HTTPS keypair")
    renew = False
    if leaf_cert_path.exists():
        leaf = x509.load_pem_x509_certificate(read_private(leaf_cert_path))
        leaf.verify_directly_issued_by(ca)
        leaf_key = serialization.load_pem_private_key(read_private(leaf_key_path), password=None)
        leaf_public = leaf.public_key()
        if not isinstance(leaf_key, ec.EllipticCurvePrivateKey) or not isinstance(
            leaf_public, ec.EllipticCurvePublicKey
        ):
            raise ValueError("Unexpected local HTTPS key")
        if leaf_key.public_key().public_numbers() != leaf_public.public_numbers():
            raise ValueError("Local HTTPS key does not match certificate")
        if leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError("Local HTTPS certificate cannot be a CA")
        if not leaf.extensions.get_extension_for_class(x509.KeyUsage).value.digital_signature:
            raise ValueError("Local HTTPS key must support digital signatures")
        leaf.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
        leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        if (
            ExtendedKeyUsageOID.SERVER_AUTH
            not in leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        ):
            raise ValueError("Local HTTPS certificate must authenticate a server")
        if leaf.not_valid_before_utc > now:
            raise ValueError("Local HTTPS certificate is not yet valid")
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if names.get_values_for_type(x509.DNSName) != [HOST]:
            raise ValueError("Local HTTPS certificate origin changed")
        renew = leaf.not_valid_after_utc < now + timedelta(days=30)
    if not leaf_cert_path.exists() or renew:
        leaf_key = ec.generate_private_key(ec.SECP256R1())
        leaf = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)]))
            .issuer_name(ca.subject)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(True, False, False, False, False, False, False, False, False),
                critical=True,
            )
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        write_private(leaf_key_path, key_bytes(leaf_key), replace=renew)
        write_private(leaf_cert_path, leaf.public_bytes(serialization.Encoding.PEM), replace=renew)

"""Private S3 configuration and idempotent bucket verification for local Compose."""

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

from infra.bootstrap.secrets import read_private, write_private

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

BUCKET = "evidencedesk"


def configure(root: Path) -> None:
    operator = root / "operator"

    def credential(name: str) -> str:
        return read_private(operator / name).decode().strip()

    config = {
        "identities": [
            {
                "name": "bootstrap",
                "credentials": [
                    {
                        "accessKey": credential("s3_admin_access_key"),
                        "secretKey": credential("s3_admin_secret_key"),
                    }
                ],
                "actions": ["Admin", "Read", "Write", "List"],
            },
            {
                "name": "application",
                "credentials": [
                    {
                        "accessKey": credential("s3_access_key"),
                        "secretKey": credential("s3_secret_key"),
                    }
                ],
                "actions": ["Read:" + BUCKET, "Write:" + BUCKET, "List:" + BUCKET],
            },
        ]
    }
    write_private(root / "storage" / "s3.json", json.dumps(config).encode())
    # Only the S3 listener binds the container interface. Filer/master/volume
    # endpoints stay on loopback so raw object access cannot bypass S3 credentials.
    write_private(
        root / "storage" / "filer.toml", b'[leveldb2]\nenabled = true\ndir = "/data/filer"\n'
    )
    qdrant = {
        "telemetry_disabled": True,
        "storage": {"storage_path": "/qdrant/storage", "snapshots_path": "/qdrant/snapshots"},
        "service": {
            "api_key": credential("qdrant_write_key"),
            "read_only_api_key": credential("qdrant_read_key"),
            "enable_cors": False,
        },
    }
    # JSON is a YAML subset; Qdrant reads this via --config-path.
    write_private(root / "qdrant" / "local.yaml", json.dumps(qdrant).encode())


def client(endpoint: str, root: Path) -> S3Client:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=read_private(root / "s3_admin_access_key").decode().strip(),
        aws_secret_access_key=read_private(root / "s3_admin_secret_key").decode().strip(),
        config=Config(
            signature_version="s3v4",
            connect_timeout=3,
            read_timeout=15,
            retries={"max_attempts": 2},
            proxies={},
            s3={"addressing_style": "path"},
        ),
    )


def initialize(root: Path, endpoint: str = "http://s3:8333") -> None:
    connection = client(endpoint, root)
    anonymous = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        config=Config(
            signature_version=UNSIGNED,
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 0},
            proxies={},
            s3={"addressing_style": "path"},
        ),
    )
    key = "bootstrap-check/" + uuid4().hex
    try:
        try:
            connection.head_bucket(Bucket=BUCKET)
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise
            connection.create_bucket(Bucket=BUCKET)
        if connection.get_bucket_versioning(Bucket=BUCKET).get("Status"):
            raise ValueError("Lifecycle requires an unversioned bucket")
        payload = b"EvidenceDesk private-storage capability check\n"
        connection.put_object(Bucket=BUCKET, Key=key, Body=payload, IfNoneMatch="*")
        try:
            connection.put_object(
                Bucket=BUCKET, Key=key, Body=b"must not overwrite", IfNoneMatch="*"
            )
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                raise
        else:
            raise ValueError("Object store did not enforce immutable conditional writes")
        response = connection.get_object(Bucket=BUCKET, Key=key)
        with response["Body"] as stream:
            if stream.read() != payload:
                raise ValueError("Object store round trip differs")
        for operation in (
            lambda: anonymous.list_objects_v2(Bucket=BUCKET, MaxKeys=1),
            lambda: anonymous.get_object(Bucket=BUCKET, Key=key),
            lambda: anonymous.put_object(Bucket=BUCKET, Key=key + "/anonymous", Body=b"denied"),
            lambda: anonymous.delete_object(Bucket=BUCKET, Key=key),
        ):
            try:
                operation()
            except ClientError as exc:
                if exc.response["ResponseMetadata"]["HTTPStatusCode"] not in (401, 403):
                    raise
            else:
                raise ValueError("Anonymous object access is enabled")
    finally:
        try:
            connection.delete_object(Bucket=BUCKET, Key=key)
            connection.delete_object(Bucket=BUCKET, Key=key + "/anonymous")
        finally:
            connection.close()
            anonymous.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secrets", type=Path, default=Path("/run/secrets"))
    parser.add_argument("--endpoint", default="http://s3:8333")
    args = parser.parse_args()
    try:
        initialize(args.secrets, args.endpoint)
    except Exception:
        raise SystemExit("Private object-store bootstrap failed its capability checks.") from None
    print("Private, unversioned object store verified; conditional writes enforced.")


if __name__ == "__main__":
    main()

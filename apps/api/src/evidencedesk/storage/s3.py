"""Private S3 objects. Call blocking SDK operations from a worker thread."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from evidencedesk.errors import Problem

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import ListObjectsV2RequestTypeDef


@dataclass(frozen=True)
class StoredObject:
    key: str
    modified: datetime
    size: int


class S3Store:
    def __init__(self, endpoint: str, region: str, bucket: str, access_key: str, secret_key: str):
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("Invalid operator object-store origin")
        if parsed.scheme == "http" and parsed.hostname not in (
            "localhost",
            "127.0.0.1",
            "::1",
            "s3",
            "moto",
        ):
            raise ValueError(
                "HTTP storage is restricted to loopback or the private Compose service"
            )
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address and (address.is_link_local or address.is_unspecified or address.is_multicast):
            raise ValueError("Unsafe object-store endpoint")
        self.bucket = bucket
        self.client: S3Client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                signature_version="s3v4",
                connect_timeout=3,
                read_timeout=15,
                retries={"max_attempts": 1, "mode": "standard"},
                proxies={},
                s3={"addressing_style": "path"},
            ),
        )

    def close(self) -> None:
        self.client.close()

    def put(self, key: str, path: Path, mime: str, sha256: str) -> None:
        try:
            with path.open("rb") as stream:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=stream,
                    ContentType=mime,
                    ContentLength=path.stat().st_size,
                    Metadata={"sha256": sha256},
                    ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode(),
                    IfNoneMatch="*",
                )
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] == 412:
                head = self.client.head_object(Bucket=self.bucket, Key=key)
                if (
                    head.get("Metadata", {}).get("sha256") == sha256
                    and head.get("ContentLength") == path.stat().st_size
                ):
                    return
                raise Problem(
                    409, "object_conflict", "Immutable object checksum conflicts."
                ) from exc
            raise Problem(
                503, "storage_unavailable", "Object storage is temporarily unavailable.", True
            ) from exc
        except BotoCoreError as exc:
            raise Problem(
                503, "storage_unavailable", "Object storage is temporarily unavailable.", True
            ) from exc

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except (ClientError, BotoCoreError) as exc:
            raise Problem(
                503, "storage_unavailable", "Object cleanup is temporarily unavailable.", True
            ) from exc

    def require_unversioned(self) -> None:
        """A delete marker does not erase older versions; refuse unsupported buckets."""
        try:
            if self.client.get_bucket_versioning(Bucket=self.bucket).get("Status"):
                raise Problem(
                    409,
                    "versioned_storage_unsupported",
                    "Object purge requires an unversioned bucket.",
                )
        except (ClientError, BotoCoreError) as exc:
            raise Problem(
                503,
                "storage_unavailable",
                "Object storage configuration could not be verified.",
                True,
            ) from exc

    def download(self, key: str, path: Path, sha256: str, limit: int = 20 * 1024 * 1024) -> None:
        digest = hashlib.sha256()
        size = 0
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            with response["Body"] as source, path.open("wb") as target:
                while part := source.read(64 * 1024):
                    size += len(part)
                    if size > limit:
                        raise Problem(
                            422, "stored_object_oversized", "Stored object exceeded its limit."
                        )
                    digest.update(part)
                    target.write(part)
            if digest.hexdigest() != sha256:
                raise Problem(
                    422, "object_checksum_mismatch", "Stored object checksum did not match."
                )
        except (ClientError, BotoCoreError) as exc:
            raise Problem(
                503, "storage_unavailable", "Original document could not be read.", True
            ) from exc

    def list_page(
        self, prefix: str, token: str | None = None, *, limit: int = 1000
    ) -> tuple[list[StoredObject], str | None]:
        if not 1 <= limit <= 1000:
            raise ValueError("Object inventory pages must contain1-1000 entries")
        try:
            params: ListObjectsV2RequestTypeDef = {
                "Bucket": self.bucket,
                "Prefix": prefix,
                "MaxKeys": limit,
            }
            if token:
                params["ContinuationToken"] = token
            response = self.client.list_objects_v2(**params)
            objects = [
                StoredObject(row["Key"], row["LastModified"], row["Size"])
                for row in response.get("Contents", [])
                if "Key" in row and "LastModified" in row and "Size" in row
            ]
            return objects, response.get("NextContinuationToken")
        except (ClientError, BotoCoreError) as exc:
            raise Problem(
                503, "storage_unavailable", "Object inventory is unavailable.", True
            ) from exc

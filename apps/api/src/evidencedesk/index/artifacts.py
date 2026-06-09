"""Read only immutable artifacts bound to a server-owned manifest and source namespace."""

import asyncio
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from evidencedesk.embeddings.models import VectorArtifact
from evidencedesk.errors import Problem
from evidencedesk.ingestion.profile import IngestionProfile
from evidencedesk.storage.s3 import S3Store


async def verify_original(store: S3Store, row: dict[str, Any]) -> None:
    prefix = f"workspaces/{row['workspace_id']}/documents/{row['document_id']}/revisions/"
    suffix = f"/original/{row['content_sha256']}"
    key = row["object_key"]
    if (
        not key.startswith(prefix)
        or not key.endswith(suffix)
        or not re.fullmatch(r"[0-9a-f-]{36}", key[len(prefix) : -len(suffix)])
    ):
        raise Problem(422, "invalid_manifest", "The original source namespace is invalid.")
    with TemporaryDirectory(prefix="ed-source-audit-") as directory:
        path = Path(directory) / "original"
        await asyncio.to_thread(store.download, key, path, row["content_sha256"], 20 * 1024 * 1024)
        if path.stat().st_size != row["byte_size"]:
            raise Problem(422, "source_integrity_failure", "The original source length changed.")


async def load_artifact(store: S3Store, row: dict[str, Any]) -> VectorArtifact:
    sha, key = row["manifest_sha256"], row["artifact_key"]
    prefix = f"workspaces/{row['workspace_id']}/documents/{row['document_id']}/revisions/{row['revision_id']}/manifest/"
    if not isinstance(sha, str) or len(sha) != 64 or key != prefix + sha:
        raise Problem(422, "invalid_manifest", "The vector artifact namespace is invalid.")
    with TemporaryDirectory(prefix="ed-index-artifact-") as directory:
        path = Path(directory) / "vectors.json"
        await asyncio.to_thread(store.download, key, path, sha, 128 * 1024 * 1024)

        def parse() -> VectorArtifact:
            try:
                value = VectorArtifact.model_validate_json(path.read_bytes())
                if value.sha256 != sha:
                    raise ValueError("Noncanonical vector artifact")
                return value
            except ValueError as exc:
                raise Problem(
                    422, "invalid_vectors", "The vector artifact failed integrity checks."
                ) from exc

        value = await asyncio.to_thread(parse)
    profile = IngestionProfile()
    if (
        (
            value.workspace_id,
            value.kb_id,
            value.document_id,
            value.revision_id,
            value.profile_id,
            value.profile_hash,
            len(value.points),
        )
        != (
            row["workspace_id"],
            row["kb_id"],
            row["document_id"],
            row["revision_id"],
            row["profile_id"],
            row["profile_hash"],
            row["expected_points"],
        )
        or value.profile_hash != profile.fingerprint
        or value.profile_id != profile.embedding_profile_id
    ):
        raise Problem(
            422, "vector_identity_mismatch", "The vector artifact does not match its manifest."
        )
    return value

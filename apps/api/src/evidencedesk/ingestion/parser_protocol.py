"""Private length-prefixed Unix-socket protocol; never a browser or network API."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

HEADER_LIMIT = 4096
SOURCE_LIMIT = 20 * 1024 * 1024
MANIFEST_LIMIT = 256 * 1024 * 1024
PROTOCOL_VERSION = 1


def envelope(raw: bytes) -> dict[str, Any]:
    if not 0 < len(raw) <= HEADER_LIMIT:
        raise ValueError("Invalid parser envelope length")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate parser envelope key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except RecursionError as exc:
        raise ValueError("Parser envelope nesting exceeds its limit") from exc
    if not isinstance(value, dict):
        raise ValueError("Invalid parser envelope")
    return value


def validate_request(request: dict[str, Any], fingerprint: str) -> None:
    if set(request) != {"version", "operation", "fingerprint", "title", "mime", "sha256", "size"}:
        raise ValueError("Invalid parser request fields")
    if type(request["version"]) is not int or request["version"] != PROTOCOL_VERSION:
        raise ValueError("Unsupported parser protocol")
    if request["operation"] != "parse" or request["fingerprint"] != fingerprint:
        raise ValueError("Parser implementation does not match")
    if type(request["size"]) is not int or not 0 < request["size"] <= SOURCE_LIMIT:
        raise ValueError("Invalid parser input length")
    if not isinstance(request["sha256"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", request["sha256"]
    ):
        raise ValueError("Invalid parser input digest")
    for name in ("title", "mime"):
        if not isinstance(request[name], str) or not 0 < len(request[name]) <= 255:
            raise ValueError("Invalid parser input metadata")
    if any(ord(c) < 32 for c in request["title"] + request["mime"]):
        raise ValueError("Invalid parser input metadata")


def service_fingerprint(lock_path: Path, root: Path = Path(".")) -> str:
    """Verify immutable parser/build inputs in both worker and parser container."""
    lock = json.loads(lock_path.read_text())
    if set(lock) != {"protocol", "source_sha256", "files"} or lock["protocol"] != PROTOCOL_VERSION:
        raise ValueError("Unsupported parser service lock")
    if not isinstance(lock["files"], dict) or not lock["files"]:
        raise ValueError("Empty parser service lock")
    for filename, digest in lock["files"].items():
        name = Path(filename)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError("Invalid parser build input")
        if filename.startswith("evidencedesk/"):
            source = root / "apps/api/src" / name
        elif filename in {"Dockerfile.service", "requirements.txt"}:
            source = root / "infra/parser" / name
        else:
            raise ValueError("Unexpected parser build input")
        if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise ValueError("Parser service inputs differ from their lock")
    fingerprint = hashlib.sha256(json.dumps(lock["files"], sort_keys=True).encode()).hexdigest()
    if fingerprint != lock["source_sha256"]:
        raise ValueError("Invalid parser service fingerprint")
    return fingerprint

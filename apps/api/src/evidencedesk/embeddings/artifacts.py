"""Offline artifact validation. Bootstrap is separate from request/worker execution."""

import hashlib
import json
from pathlib import Path
from typing import Any


def verified_snapshot(cache: Path, lock_path: Path, kind: str) -> tuple[Path, dict[str, Any]]:
    lock = json.loads(lock_path.read_text())
    record = next(m for m in lock["models"] if m["kind"] == kind)
    root = cache / ("fastembed" if kind == "sparse" else "")
    snapshot = (
        root
        / ("models--" + record["model_id"].replace("/", "--"))
        / "snapshots"
        / record["revision"]
    )
    for name, expected in record["files_sha256"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid artifact path in operator lock")
        with (snapshot / relative).open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError(f"{kind} artifact integrity check failed")
    if not record["files_sha256"]:
        raise ValueError("Empty artifact manifest")
    return snapshot, dict(record)

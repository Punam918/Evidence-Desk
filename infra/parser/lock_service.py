"""Update the reviewed service source lock; run before building after source changes."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULES = [
    "errors.py",
    "ingestion/validation.py",
    "ingestion/normalize.py",
    "ingestion/blocks.py",
    "ingestion/sandbox.py",
    "ingestion/parsers/core.py",
    "ingestion/parser_protocol.py",
    "ingestion/parser_isolation.py",
    "ingestion/parser_service_child.py",
    "ingestion/parser_service.py",
    "ingestion/parser_client.py",
]
files = {
    "evidencedesk/" + module: hashlib.sha256(
        (ROOT / "apps/api/src/evidencedesk" / module).read_bytes()
    ).hexdigest()
    for module in MODULES
}
for name in ("Dockerfile.service", "requirements.txt"):
    files[name] = hashlib.sha256((ROOT / "infra/parser" / name).read_bytes()).hexdigest()
fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
lock = {"protocol": 1, "source_sha256": fingerprint, "files": files}
(ROOT / "infra/parser/service.lock.json").write_text(json.dumps(lock, indent=2) + "\n")
print(fingerprint)

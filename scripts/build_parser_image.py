"""Build the small parser from selected sources and hash-locked wheels; never send secrets as context."""
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["errors.py", "ingestion/validation.py", "ingestion/normalize.py", "ingestion/blocks.py",
         "ingestion/parsers/core.py", "ingestion/parser_entry.py"]
with tempfile.TemporaryDirectory(prefix="ed-parser-build-") as temp:
    context = Path(temp)
    for name in ("Dockerfile", "requirements.txt"):
        shutil.copyfile(ROOT / "infra/parser" / name, context / name)
    for name in FILES:
        target = context / "evidencedesk" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "apps/api/src/evidencedesk" / name, target)
    manifest = {str(path.relative_to(context)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(context.rglob('*')) if path.is_file()}
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    subprocess.run(["docker", "build", "--tag", "evidencedesk-parser:" + digest[:16], "--iidfile", str(context / "image-id"), str(context)], check=True)
    lock = {"image_id": (context / "image-id").read_text().strip(), "source_sha256": digest, "files": manifest,
            "python": "3.14.8", "parser_version": "parse_v1"}
    (ROOT / "infra/parser/image.lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    print(json.dumps({"image_id": lock["image_id"], "source_sha256": digest}))

"""Build only source-locked parser files, record the immutable local output image ID."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from evidencedesk.ingestion.parser_protocol import service_fingerprint

ROOT = Path(__file__).resolve().parents[2]
lock_path = ROOT / "infra/parser/service.lock.json"
fingerprint = service_fingerprint(lock_path, ROOT)
lock = json.loads(lock_path.read_text())
with tempfile.TemporaryDirectory(prefix="ed-parser-service-build-") as directory:
    context = Path(directory)
    paths = [Path("infra/parser/service.lock.json")]
    for filename in lock["files"]:
        paths.append(
            Path("apps/api/src" if filename.startswith("evidencedesk/") else "infra/parser")
            / filename
        )
    for path in paths:
        target = context / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, target)
    subprocess.run(
        [
            "docker",
            "build",
            "-f",
            "infra/parser/Dockerfile.service",
            "--tag",
            "evidencedesk-parser-service:local",
            "--iidfile",
            str(context / "image-id"),
            str(context),
        ],
        check=True,
    )
    if service_fingerprint(lock_path, ROOT) != fingerprint:
        raise RuntimeError("Parser inputs changed during build")
    output = {"image_id": (context / "image-id").read_text().strip(), "source_sha256": fingerprint}
    (ROOT / "infra/parser/service-build.lock.json").write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output))

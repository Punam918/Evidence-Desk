"""Build/source identity for the local application images; no credentials in the manifest."""

import argparse
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "infra/application/build.lock.json"
DIRECTORIES = (
    "apps/api/src",
    "apps/api/migrations",
    "apps/web/src",
    "apps/web/public",
    "packages/api-client",
    "contracts/config",
    "contracts/schemas",
    "infra/bootstrap",
)
FILES = (
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "alembic.ini",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "apps/api/pyproject.toml",
    "apps/web/package.json",
    "apps/web/index.html",
    "apps/web/tsconfig.json",
    "apps/web/vite.config.ts",
    "infra/application/Dockerfile.api",
    "infra/application/Dockerfile.web",
    "infra/application/Dockerfile.identity",
    "infra/application/Caddyfile",
    "infra/application/Caddyfile.local",
    "infra/application/evidencedesk",
    "infra/application/images.lock.json",
    "infra/parser/image.lock.json",
    "infra/parser/Dockerfile",
    "infra/parser/requirements.txt",
    "infra/parser/Dockerfile.service",
    "infra/parser/service.lock.json",
    "scripts/setup_checkpoints.py",
    "scripts/prepare_models.py",
)


def inputs(root: Path = ROOT) -> dict[str, str]:
    paths = {root / name for name in FILES}
    for name in DIRECTORIES:
        directory = root / name
        if name.endswith("/public") and not directory.exists():
            continue
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError("Expected build source directory missing or symbolic")
        for path in directory.rglob("*"):
            if any(
                part in ("__pycache__", "node_modules") or part.startswith(".")
                for part in path.relative_to(directory).parts
            ):
                continue
            if path.is_symlink():
                raise ValueError("Symbolic build sources are unsupported")
            if path.is_file() and path.suffix not in (".pyc", ".tsbuildinfo", ".pem", ".key"):
                paths.add(path)
    values = {}
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Build input escaped project root")
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Unexpected oversized build input")
        values[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return values


def verified_images(root: Path = ROOT) -> dict[str, str]:
    path = root / MANIFEST
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Application image build manifest missing or oversized")
    manifest = json.loads(path.read_text())
    if manifest.get("inputs_sha256") != inputs(root):
        raise ValueError("Application image build inputs changed; rebuild before release")
    images = manifest.get("images", {})
    if set(images) != {"application", "web", "identity"} or any(
        not isinstance(value, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value)
        for value in images.values()
    ):
        raise ValueError("Application images require exact image IDs")
    return images


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify recorded source and image identity without building",
    )
    args = parser.parse_args()
    if args.check:
        print(json.dumps(verified_images()))
        return
    before = inputs()
    images = {}
    for label, dockerfile, tag in (
        ("application", "Dockerfile.api", "evidencedesk-application:local"),
        ("web", "Dockerfile.web", "evidencedesk-web:local"),
        ("identity", "Dockerfile.identity", "evidencedesk-identity:local"),
    ):
        subprocess.run(
            ["docker", "build", "-f", "infra/application/" + dockerfile, "-t", tag, "."],
            cwd=ROOT,
            check=True,
        )
        images[label] = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", tag], text=True
        ).strip()
    if before != inputs():
        raise ValueError("Sources changed during the build; no release identity was recorded")
    manifest = {
        "version": 1,
        "built_at": datetime.now(UTC).isoformat(),
        "images": images,
        "inputs_sha256": before,
    }
    path = ROOT / MANIFEST
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(path)
    verified_images()
    print(json.dumps({"manifest": MANIFEST, "images": images}))


if __name__ == "__main__":
    main()

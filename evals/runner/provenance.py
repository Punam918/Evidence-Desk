"""Reproducible source identity from an explicit allowlist; never walk secret/cache directories."""

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from evals.curation.build import ROOT, sha
from evals.curation.validate import read_json

DIRECTORIES = (
    "apps/api/src",
    "apps/api/migrations",
    "apps/web/src",
    "evals/runner",
    "evals/curation",
    "contracts",
)
SUFFIXES = {".py", ".sql", ".json", ".yaml", ".yml", ".tsx", ".ts", ".css", ".md", ".txt"}
FILES = (
    "pyproject.toml",
    "uv.lock",
    "pnpm-lock.yaml",
    "package.json",
    "apps/api/pyproject.toml",
    "apps/web/package.json",
    "infra/parser/image.lock.json",
)


def snapshot(root: Path = ROOT) -> dict[str, Any]:
    paths = {root / p for p in FILES}
    for directory in DIRECTORIES:
        base = root / directory
        if not base.is_dir() or base.is_symlink():
            raise ValueError("Expected source directory missing or symbolic")
        for p in base.rglob("*"):
            if any(
                part.startswith(".") or part == "__pycache__" for part in p.relative_to(base).parts
            ):
                continue
            if p.is_symlink():
                raise ValueError("Symbolic source paths are not supported")
            if p.is_file() and p.suffix in SUFFIXES:
                paths.add(p)
    if len(paths) > 5000:
        raise ValueError("Source manifest exceeds bounded file count")
    hashes: dict[str, str] = {}
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Source path escaped project root")
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Source file exceeds snapshot limit")
        hashes[path.relative_to(root).as_posix()] = sha(path.read_bytes())
    git_sha: str | None = None
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if Path(top).resolve() == root.resolve():
            git_sha = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
    except OSError, subprocess.SubprocessError:
        pass
    return {
        "version": 1,
        "git_sha": git_sha,
        "files": hashes,
        "scope": "allowlisted_source_and_locks",
    }


def verify(path: Path, root: Path = ROOT) -> tuple[str, str | None]:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Source manifest exceeds limit")
    content = path.read_bytes()
    manifest = read_json(content.decode("utf-8"))
    current = snapshot(root)
    if manifest != current:
        raise ValueError("Source snapshot differs from current source or locks")
    return sha(content), current["git_sha"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture allowlisted source/lock identity before collecting observations"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    content = json.dumps(snapshot(), sort_keys=True, indent=2) + "\n"
    with args.output.open("x") as stream:
        stream.write(content)
    print(json.dumps({"source_snapshot_sha256": sha(content.encode()), "path": str(args.output)}))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Prepare immutable model artifacts; serving processes remain strictly offline."""

import argparse
import hashlib
import json
import os
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify_file(path: Path, expected: str) -> None:
    if not path.is_file():
        raise ValueError("Missing model artifact; run scripts/prepare_models.py --download")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise ValueError("Model artifact integrity check failed; cached data was not replaced")


def atomic_artifact(path: Path, data: bytes, expected: str) -> None:
    """Never publish partial or unverified tokenizer bytes under a serving filename."""
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Generation tokenizer download/cache failed integrity checking")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_models(cache: Path, *, download: bool, root: Path = ROOT) -> None:
    # Read by huggingface_hub at import. Compose also bounds the entire subprocess.
    # cache_dir controls the model snapshot only, not Xet's metadata/log caches.
    # Numeric container users have no home entry and expand ~ to /; keep every
    # auxiliary download write inside the explicitly selected writable cache.
    os.environ["HF_HOME"] = str(cache.resolve() / ".huggingface")
    os.environ["HF_XET_CACHE"] = str(cache.resolve() / ".huggingface/xet")
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "10")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from evidencedesk.embeddings.artifacts import verified_snapshot
    from evidencedesk.models.tokenizer import generation_tokenizer
    from huggingface_hub import hf_hub_download

    lock_path = root / "contracts/config/model-artifacts.lock.json"
    for record in json.loads(lock_path.read_text())["models"]:
        kind = record["kind"]
        model_cache = cache / ("fastembed" if kind == "sparse" else "")
        snapshot = (
            model_cache
            / ("models--" + record["model_id"].replace("/", "--"))
            / "snapshots"
            / record["revision"]
        )
        for name, expected in record["files_sha256"].items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Invalid artifact path in operator lock")
            path = snapshot / relative
            if not path.is_file() and download:
                hf_hub_download(
                    record["model_id"],
                    name,
                    revision=record["revision"],
                    cache_dir=model_cache,
                    token=False,
                    etag_timeout=10,
                )
            verify_file(path, expected)
        verified_snapshot(cache, lock_path, kind)
        print(f"{kind}: immutable snapshot {record['revision']} verified", flush=True)

    tokenizer_lock = root / "contracts/config/generation-tokenizer.lock.json"
    record = json.loads(tokenizer_lock.read_text())
    path = cache / "tiktoken/o200k_base.tiktoken"
    if not path.is_file():
        legacy = (
            Path(tempfile.gettempdir())
            / "data-gym-cache"
            / hashlib.sha1(record["url"].encode()).hexdigest()
        )
        data = legacy.read_bytes() if legacy.is_file() else None
        if data is None and download:
            with urllib.request.urlopen(record["url"], timeout=30) as response:
                data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("Generation tokenizer exceeds download limit")
        if data is None:
            raise ValueError("Missing generation tokenizer; run prepare_models.py --download")
        atomic_artifact(path, data, record["sha256"])
    verify_file(path, record["sha256"])
    generation_tokenizer(cache, tokenizer_lock)
    print("generation tokenizer: o200k_base immutable artifact verified", flush=True)

    record = json.loads((root / "contracts/config/local-tokenizer.lock.json").read_text())
    local_cache = cache / "qwen3-tokenizer"
    for name, expected in record["files_sha256"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid artifact path in operator lock")
        path = local_cache / relative
        if not path.is_file() and download:
            hf_hub_download(
                record["model_id"],
                name,
                revision=record["revision"],
                local_dir=local_cache,
                token=False,
                etag_timeout=10,
            )
        verify_file(path, expected)
    print("local generation tokenizer: immutable Qwen snapshot verified", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Download missing locked files")
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(os.environ.get("ED_MODEL_CACHE_PATH", ROOT / ".cache/models")),
    )
    args = parser.parse_args()
    try:
        prepare_models(args.cache, download=args.download)
    except Exception as error:
        # Hub/Xet/network exception bodies may contain signed download URLs.
        # Preserve the failure class without leaking those response details.
        raise SystemExit(
            "Model artifact preparation failed: "
            + type(error).__name__
            + "; verify cache permissions, connectivity, and the pinned artifact availability"
        ) from None


if __name__ == "__main__":
    main()

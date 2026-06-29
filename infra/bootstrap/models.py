"""Idempotent local model initialization, separate from offline serving workers.

Downloads require --download; an existing alias is never replaced. No model generation,
paid provider request, Docker API access, or runtime/model lock updates occur here.
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[2]
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_PULL_LINE_BYTES = 16 * 1024


class BootstrapError(RuntimeError):
    """An operator-visible failure with no secret or remote response content."""


def validate_origin(origin: str) -> str:
    parsed = urlsplit(origin)
    if (
        origin != origin.strip()
        or parsed.scheme != "http"
        or parsed.hostname not in ("ollama", "127.0.0.1", "localhost", "::1")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or parsed.port is None
        or parsed.port == 0
        or any(ord(character) < 33 for character in origin)
    ):
        raise BootstrapError("Bootstrap requires the explicit Compose or loopback Ollama origin")
    return origin.rstrip("/")


def read_lock(path: Path) -> dict[str, Any]:
    record = json.loads(path.read_text())
    if not isinstance(record, dict):
        raise BootstrapError("Local model lock must be a JSON object")
    if (
        not re.fullmatch(r"[a-f0-9]{64}", record.get("digest", ""))
        or not re.fullmatch(r"[a-z0-9._-]+:[a-z0-9._-]+", record.get("model_id", ""))
        or not re.fullmatch(r"[a-z0-9._-]+:[a-z0-9._-]+", record.get("upstream_model", ""))
        or record["model_id"].endswith(":latest")
        or record["model_id"] == record["upstream_model"]
    ):
        raise BootstrapError("Invalid immutable local model lock")
    return record


async def request_json(
    client: httpx.AsyncClient, method: str, path: str, *, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    # Bound response bytes before parsing; never log remote error bodies.
    async with client.stream(method, path, json=body) as response:
        response.raise_for_status()
        data = bytearray()
        async for part in response.aiter_bytes(chunk_size=65536):
            data.extend(part)
            if len(data) > MAX_JSON_BYTES:
                raise BootstrapError("Ollama metadata exceeds the response limit")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise BootstrapError("Ollama metadata must be a JSON object")
    return result


async def model_record(client: httpx.AsyncClient, name: str) -> dict[str, Any] | None:
    response = await request_json(client, "GET", "/api/tags")
    models = response.get("models")
    if not isinstance(models, list) or not all(isinstance(item, dict) for item in models):
        raise BootstrapError("Ollama returned an invalid model inventory")
    matches = [item for item in models if item.get("name") == name]
    if len(matches) > 1:
        raise BootstrapError("Ollama returned duplicate model aliases")
    return matches[0] if matches else None


def verify_identity(found: dict[str, Any], lock: dict[str, Any]) -> None:
    if found.get("digest") != lock["digest"] or found.get("size") != lock["size_bytes"]:
        raise BootstrapError("Local model digest/size differs from its lock; no alias was replaced")


async def pull_model(client: httpx.AsyncClient, name: str) -> None:
    complete = False
    pending = b""
    async with client.stream("POST", "/api/pull", json={"model": name, "stream": True}) as response:
        response.raise_for_status()
        async for part in response.aiter_bytes(chunk_size=MAX_PULL_LINE_BYTES):
            pending += part
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if len(line) > MAX_PULL_LINE_BYTES:
                    raise BootstrapError("Ollama pull status exceeds the response limit")
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict) or "error" in record:
                    raise BootstrapError(
                        "Ollama pull failed; retry bootstrap to resume missing layers"
                    )
                complete = complete or record.get("status") == "success"
            if len(pending) > MAX_PULL_LINE_BYTES:
                raise BootstrapError("Ollama pull status exceeds the response limit")
    if pending.strip():
        record = json.loads(pending)
        if not isinstance(record, dict) or "error" in record:
            raise BootstrapError("Ollama pull ended with an invalid status")
        complete = complete or record.get("status") == "success"
    if not complete:
        raise BootstrapError("Ollama pull ended before success; rerun bootstrap to resume")


async def prepare_ollama(
    client: httpx.AsyncClient, lock: dict[str, Any], *, download: bool
) -> None:
    version = await request_json(client, "GET", "/api/version")
    if version.get("version") != lock["runtime_version"]:
        raise BootstrapError("Ollama runtime version differs from its lock")
    current = await model_record(client, lock["model_id"])
    if current is None:
        upstream = await model_record(client, lock["upstream_model"])
        if upstream is None:
            if not download:
                raise BootstrapError(
                    "Locked local model is missing; rerun bootstrap with --download"
                )
            print("Downloading local Qwen weights; existing partial layers can resume", flush=True)
            await pull_model(client, lock["upstream_model"])
            upstream = await model_record(client, lock["upstream_model"])
        if upstream is None:
            raise BootstrapError("Ollama pull did not publish the expected upstream model")
        verify_identity(upstream, lock)
        response = await client.post(
            "/api/copy", json={"source": lock["upstream_model"], "destination": lock["model_id"]}
        )
        response.raise_for_status()
        current = await model_record(client, lock["model_id"])
    if current is None:
        raise BootstrapError("Ollama did not publish the verified local alias")
    verify_identity(current, lock)
    shown = await request_json(client, "POST", "/api/show", body={"model": lock["model_id"]})
    for field in ("template", "license"):
        value = shown.get(field)
        if (
            not isinstance(value, str)
            or hashlib.sha256(value.encode()).hexdigest() != lock[field + "_sha256"]
        ):
            raise BootstrapError("Local model template/license differs from its lock")
    if shown.get("capabilities") != lock["capabilities"]:
        raise BootstrapError("Local model capabilities differ from their lock")
    print(f"local generation: {lock['model_id']} exact digest and metadata verified", flush=True)


async def run_ollama(origin: str, lock: dict[str, Any], *, download: bool, timeout: int) -> None:
    # Read/connect timeouts alone do not bound a slow but active stream.
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(
            base_url=origin,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(60, connect=5, write=10, pool=5),
        ) as client:
            await prepare_ollama(client, lock, download=download)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    parser.add_argument(
        "--cache", type=Path, default=Path(os.getenv("ED_MODEL_CACHE_PATH", "/models"))
    )
    parser.add_argument("--ollama-url", default=os.getenv("ED_LOCAL_MODEL_URL"))
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    args = parser.parse_args()
    if not args.ollama_url:
        parser.error("--ollama-url or ED_LOCAL_MODEL_URL is required; no host service is assumed")
    if not 30 <= args.timeout_seconds <= 14400:
        parser.error("timeout-seconds must be between 30 and 14400")
    try:
        origin = validate_origin(args.ollama_url)
        lock = read_lock(ROOT / "contracts/config/local-runtime.lock.json")
        args.cache.mkdir(parents=True, exist_ok=True)
        # One bootstrap writer; fail promptly instead of waiting on another initializer.
        with (args.cache / ".bootstrap.lock").open("a") as mutex:
            fcntl.flock(mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
            command = [
                sys.executable,
                str(ROOT / "scripts/prepare_models.py"),
                "--cache",
                str(args.cache),
            ]
            if args.download:
                command.append("--download")
            subprocess.run(command, check=True, timeout=args.timeout_seconds)
            asyncio.run(
                run_ollama(origin, lock, download=args.download, timeout=args.timeout_seconds)
            )
    except (
        BootstrapError,
        ValueError,
        KeyError,
        OSError,
        httpx.HTTPError,
        TimeoutError,
        subprocess.SubprocessError,
    ) as error:
        # Do not expose arbitrary remote response bodies or URLs in failure logs.
        message = str(error) if isinstance(error, BootstrapError) else type(error).__name__
        raise SystemExit("Model bootstrap failed: " + message) from None


if __name__ == "__main__":
    main()

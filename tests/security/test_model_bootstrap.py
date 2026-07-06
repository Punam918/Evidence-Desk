"""Bootstrap never publishes a drifted local model or accepts corrupt cached bytes."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from infra.bootstrap.models import (
    BootstrapError,
    prepare_ollama,
    read_lock,
    validate_origin,
)
from scripts.prepare_models import atomic_artifact, verify_file

LOCK = {
    "runtime_version": "0.40.3",
    "model_id": "evidencedesk-qwen3-8b:500a1f067a9f7826",
    "upstream_model": "qwen3:8b",
    "digest": "a" * 64,
    "size_bytes": 12345,
    "template_sha256": hashlib.sha256(b"template").hexdigest(),
    "license_sha256": hashlib.sha256(b"license").hexdigest(),
    "capabilities": ["completion", "tools", "thinking"],
}


class Runtime:
    def __init__(self, *, alias=False, upstream=False):
        self.models = []
        if alias:
            self.add(LOCK["model_id"])
        if upstream:
            self.add(LOCK["upstream_model"])
        self.calls = []
        self.version = LOCK["runtime_version"]
        self.digest = LOCK["digest"]
        self.template = "template"
        self.pull_status = b'{"status":"success"}\n'

    def add(self, name):
        self.models.append({"name": name, "digest": LOCK["digest"], "size": LOCK["size_bytes"]})

    def __call__(self, request):
        path = request.url.path
        self.calls.append(path)
        if path == "/api/version":
            return httpx.Response(200, json={"version": self.version})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": self.models})
        if path == "/api/pull":
            assert json.loads(request.content) == {"model": LOCK["upstream_model"], "stream": True}
            self.models.append(
                {"name": LOCK["upstream_model"], "digest": self.digest, "size": LOCK["size_bytes"]}
            )
            return httpx.Response(200, content=self.pull_status)
        if path == "/api/copy":
            assert json.loads(request.content) == {
                "source": LOCK["upstream_model"],
                "destination": LOCK["model_id"],
            }
            self.add(LOCK["model_id"])
            return httpx.Response(200)
        if path == "/api/show":
            return httpx.Response(
                200,
                json={
                    "template": self.template,
                    "license": "license",
                    "capabilities": LOCK["capabilities"],
                },
            )
        raise AssertionError("Unexpected bootstrap endpoint " + path)


async def initialize(runtime, *, download=True):
    async with httpx.AsyncClient(
        base_url="http://ollama:11434", transport=httpx.MockTransport(runtime)
    ) as client:
        await prepare_ollama(client, LOCK, download=download)


async def test_first_install_and_rerun_only_pull_and_copy_once():
    runtime = Runtime()
    await initialize(runtime)
    await initialize(runtime)
    assert runtime.calls.count("/api/pull") == 1
    assert runtime.calls.count("/api/copy") == 1
    assert runtime.calls.count("/api/show") == 2


async def test_cached_upstream_can_initialize_without_a_download():
    runtime = Runtime(upstream=True)
    await initialize(runtime, download=False)
    assert "/api/pull" not in runtime.calls
    assert "/api/copy" in runtime.calls


async def test_missing_model_without_download_has_no_mutating_requests():
    runtime = Runtime()
    with pytest.raises(BootstrapError, match="missing"):
        await initialize(runtime, download=False)
    assert set(runtime.calls) <= {"/api/version", "/api/tags"}


async def test_drifted_upstream_is_never_promoted_to_locked_alias():
    runtime = Runtime()
    runtime.digest = "b" * 64
    with pytest.raises(BootstrapError, match="digest/size differs"):
        await initialize(runtime)
    assert "/api/copy" not in runtime.calls
    assert not any(item["name"] == LOCK["model_id"] for item in runtime.models)


@pytest.mark.parametrize("field,value", [("digest", "b" * 64), ("size", 100)])
async def test_corrupt_existing_alias_is_not_replaced(field, value):
    runtime = Runtime(alias=True)
    runtime.models[0][field] = value
    with pytest.raises(BootstrapError, match="digest/size differs"):
        await initialize(runtime)
    assert "/api/pull" not in runtime.calls and "/api/copy" not in runtime.calls


async def test_runtime_drift_stops_before_model_writes():
    runtime = Runtime()
    runtime.version = "unexpected"
    with pytest.raises(BootstrapError, match="runtime version differs"):
        await initialize(runtime)
    assert runtime.calls == ["/api/version"]


async def test_template_drift_rejects_existing_alias():
    runtime = Runtime(alias=True)
    runtime.template = "changed"
    with pytest.raises(BootstrapError, match="template/license differs"):
        await initialize(runtime)


@pytest.mark.parametrize(
    "status", [b'{"status":"downloading"}\n', b'{"error":"remote secret"}\n', b"x" * 16385]
)
async def test_interrupted_or_invalid_pull_does_not_copy(status):
    runtime = Runtime()
    runtime.pull_status = status
    with pytest.raises(BootstrapError) as error:
        await initialize(runtime)
    assert "remote secret" not in str(error.value)
    assert "/api/copy" not in runtime.calls


@pytest.mark.parametrize(
    "origin", ["http://ollama:11434", "http://127.0.0.1:23456/", "http://[::1]:23456"]
)
def test_explicit_local_origin(origin):
    assert validate_origin(origin) == origin.rstrip("/")


@pytest.mark.parametrize(
    "origin",
    [
        "http://ollama",
        "http://evil:11434",
        "http://u:p@ollama:11434",
        "http://ollama:11434/api",
        "http://ollama:11434?key=x",
        "http://ollama:0",
        "\nhttp://ollama:11434",
    ],
)
def test_bootstrap_cannot_be_redirected_to_an_arbitrary_origin(origin):
    with pytest.raises(BootstrapError):
        validate_origin(origin)


def test_atomic_tokenizer_publish_and_bad_download_leave_existing_file_intact(tmp_path):
    path = tmp_path / "tokenizer"
    digest = hashlib.sha256(b"approved").hexdigest()
    atomic_artifact(path, b"approved", digest)
    verify_file(path, digest)
    with pytest.raises(ValueError, match="integrity"):
        atomic_artifact(path, b"wrong", digest)
    assert path.read_bytes() == b"approved"
    assert list(tmp_path.iterdir()) == [path]
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="cached data was not replaced"):
        verify_file(path, digest)
    assert path.read_bytes() == b"corrupt"


def test_repo_lock_is_accepted_and_mutable_alias_rejected(tmp_path):
    original = read_lock(Path("contracts/config/local-runtime.lock.json"))
    original["model_id"] = "evidencedesk:latest"
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(original))
    with pytest.raises(BootstrapError, match="immutable"):
        read_lock(path)


def test_download_library_auxiliary_caches_follow_selected_volume(tmp_path):
    # Use a fresh process: huggingface_hub snapshots environment variables at
    # import. Stop before any artifact download by pointing at a missing lock.
    source = """import json,os,sys
from pathlib import Path
from scripts.prepare_models import prepare_models
cache=Path(sys.argv[1]); os.environ['HF_HOME']='/.cache/unwritable'
os.environ['HF_XET_CACHE']='/.cache/unwritable/xet'
try:
    prepare_models(cache,download=False,root=cache/'missing-contracts')
except FileNotFoundError:
    pass
from huggingface_hub import constants
print(json.dumps({'home':constants.HF_HOME,'xet':constants.HF_XET_CACHE}))
"""
    result = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(result.stdout) == {
        "home": str(tmp_path / ".huggingface"),
        "xet": str(tmp_path / ".huggingface/xet"),
    }


def test_download_cli_does_not_disclose_remote_error_details(monkeypatch, capsys):
    from scripts import prepare_models

    def fail(*args, **kwargs):
        raise OSError("https://storage.example/model?signature=secret-download-token")

    monkeypatch.setattr(prepare_models, "prepare_models", fail)
    monkeypatch.setattr(sys, "argv", ["prepare_models.py", "--download"])
    with pytest.raises(SystemExit, match="OSError") as failure:
        prepare_models.main()
    assert "signature" not in str(failure.value)
    assert "secret-download-token" not in str(failure.value)
    assert capsys.readouterr() == ("", "")

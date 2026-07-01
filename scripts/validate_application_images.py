"""Run real container packaging checks; does not claim a working integrated application."""

import argparse
import hashlib
import json
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

if __package__:
    from .application_images import verified_images
else:
    from application_images import verified_images

ROOT = Path(__file__).resolve().parents[1]
PYTHON = (
    "python:3.14.8-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170"
)


def command(*args: str, source: str | None = None, timeout: int = 120) -> str:
    arguments = list(args)
    cleanup = None
    if arguments[0] == "run" and "--rm" in arguments:
        cleanup = "ed-image-once-" + uuid4().hex[:12]
        arguments[1:1] = ["--name", cleanup, "--no-healthcheck"]
    try:
        return subprocess.run(
            ["docker", *arguments],
            input=source,
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout,
        ).stdout.strip()
    finally:
        if cleanup:
            subprocess.run(["docker", "rm", "-f", cleanup], capture_output=True, timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new report path; evidence is not overwritten")
    images = {}
    for label, name in verified_images(ROOT).items():
        metadata = json.loads(command("image", "inspect", name))[0]
        assert metadata["Config"]["User"] == "10001:10001"
        images[label] = {
            "image_id": metadata["Id"],
            "size_bytes": metadata["Size"],
            "user": metadata["Config"]["User"],
            "os": metadata["Os"],
            "architecture": metadata["Architecture"],
        }
    api_id, web_id = images["application"]["image_id"], images["web"]["image_id"]
    isolation = [
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=128",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=268435456",
    ]
    assert "chat-worker" in command("run", "--rm", "--network=none", *isolation, api_id, "--help")
    inference = command(
        "run",
        "--rm",
        "-i",
        "--network=none",
        "--memory=3g",
        "--cpus=4",
        *isolation,
        "--mount",
        f"type=bind,src={ROOT / '.cache/models'},dst=/app/.cache/models,readonly",
        "--entrypoint=python",
        api_id,
        "-I",
        "-",
        timeout=120,
        source="""import asyncio, importlib.util, json, math, os
from pathlib import Path
from uuid import UUID
from evidencedesk.app import create_app
from evidencedesk.embeddings.encoder import Encoder
from evidencedesk.providers.local_tokens import LocalTokens
from evidencedesk.models.tokenizer import generation_tokenizer
from evidencedesk.retrieval.rerank_process import RerankProcess
from evidencedesk.retrieval.rerank_protocol import ScoreRequest, Pair
from evidencedesk.workers.runtime import parser_image
from evidencedesk.prompts import VERSION, instructions
assert os.getuid() == 10001
assert importlib.util.find_spec("pytest") is None
assert not Path("/app/.env").exists()
assert not Path("/app/infra/secrets").exists()
assert parser_image(Path("infra/parser/image.lock.json")).startswith("sha256:")
try:
    Path("/app/unexpected-write").write_text("forbidden")
except OSError:
    pass
else:
    raise AssertionError("Runtime root must be read-only")
cache, lock = Path(".cache/models"), Path("contracts/config/model-artifacts.lock.json")
model = Encoder(cache, lock)
vector = model.query_dense("How are immutable document revisions retained?")
assert len(vector) == 384 and all(math.isfinite(x) for x in vector)
assert abs(sum(x*x for x in vector) - 1) < 0.001
assert model.query_sparse("immutable revisions").indices
assert generation_tokenizer().encode("frozen tokenizer")
assert LocalTokens(cache / "qwen3-tokenizer", Path("contracts/config/local-tokenizer.lock.json"))
async def score():
    process = RerankProcess(cache, lock)
    try:
        await process.start()
        response = await process.score(ScoreRequest(query="What is retained?", passages=[Pair(id=UUID(int=1), text="Audit records are retained."), Pair(id=UUID(int=2), text="Bananas grow on plants.")]))
        assert len(response.scores) == 2 and all(math.isfinite(s.logit) for s in response.scores)
        return response.fingerprint
    finally:
        await process.close()
print(json.dumps({"dense_dimensions": len(vector), "sparse_nonempty": True, "reranker_fingerprint": asyncio.run(score()), "prompt_version": VERSION, "prompt_sha256": instructions(False)[1], "network": "none", "generation_calls": 0}))
""",
    )
    # The final stdout line is a fixed aggregate; model initialization may print progress.
    inference_result = json.loads(inference.splitlines()[-1])
    name = "ed-image-check-" + uuid4().hex[:12]
    web, upstream, network = name + "-web", name + "-upstream", name + "-net"
    checks = {}
    try:
        # A normal isolated project bridge is required for the host-published loopback port.
        # Inference above has --network=none; this bridge carries synthetic routing fixtures only.
        command("network", "create", network)
        command(
            "run",
            "-d",
            "--name",
            upstream,
            "--network",
            network,
            "--network-alias",
            "api",
            "--network-alias",
            "identity",
            "--user=10001:10001",
            *isolation,
            PYTHON,
            "python",
            "-c",
            "from http.server import BaseHTTPRequestHandler,HTTPServer\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            " def log_message(self,*args): pass\n"
            " def do_GET(self):\n"
            "  self.send_response(200)\n"
            '  self.send_header("Content-Type","text/event-stream" if self.path.endswith("/events") else "text/plain")\n'
            "  self.end_headers()\n"
            '  self.wfile.write(("data: stream-check\\n\\n" if self.path.endswith("/events") else self.path).encode())\n'
            'HTTPServer(("0.0.0.0",8080),Handler).serve_forever()\n',
        )
        command(
            "run",
            "-d",
            "--name",
            web,
            "--network",
            network,
            "-p",
            "127.0.0.1::8080",
            *isolation,
            "--tmpfs",
            "/data:rw,nosuid,nodev,size=16777216,uid=10001,gid=10001",
            "--tmpfs",
            "/config:rw,nosuid,nodev,size=16777216,uid=10001,gid=10001",
            web_id,
        )
        try:
            port = command("port", web, "8080/tcp").rsplit(":", 1)[1]
        except subprocess.CalledProcessError as exc:
            logs = subprocess.run(["docker", "logs", web], capture_output=True, text=True)
            raise RuntimeError("Synthetic web check failed: " + logs.stdout + logs.stderr) from exc
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False
        ) as client:
            for _ in range(60):
                try:
                    if client.get("/web-healthz").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.25)
            else:
                raise RuntimeError("Web image did not become healthy")
            index = client.get("/")
            assert index.status_code == 200 and '<div id="root">' in index.text
            assert index.headers["cache-control"] == "no-cache"
            assert index.headers["x-content-type-options"] == "nosniff"
            assert client.get("/workspace/deep/link").text == index.text
            asset = re.search(r'src="(/assets/[^" ]+\.js)"', index.text)
            assert asset
            js = client.get(asset.group(1))
            assert js.status_code == 200 and "immutable" in js.headers["cache-control"]
            assert client.get("/assets/missing.js").status_code == 404
            for path in (
                "/v1/me",
                "/auth/login?next=test",
                "/identity/realms/example",
                "/health/live",
            ):
                assert client.get(path).text == path
            assert client.get("/v1/runs/example/events").text == "data: stream-check\n\n"
            checks = {
                "spa_entry_and_deep_link": True,
                "asset_cache_and_missing_asset_404": True,
                "headers": True,
                "api_identity_paths_preserved": True,
                "sse_body": True,
            }
    finally:
        for resource in (web, upstream):
            subprocess.run(["docker", "rm", "-f", resource], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)
    inputs = [
        ".dockerignore",
        "uv.lock",
        "pnpm-lock.yaml",
        "infra/application/Dockerfile.api",
        "infra/application/Dockerfile.web",
        "infra/application/Caddyfile",
        "infra/application/evidencedesk",
        "infra/application/images.lock.json",
        "infra/application/build.lock.json",
        "scripts/validate_application_images.py",
    ]
    report = {
        "scope": "real_container_packaging_smoke",
        "recorded_at": datetime.now(UTC).isoformat(),
        "images": images,
        "inference": inference_result,
        "web_checks": checks,
        "inputs_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in inputs},
        "limitations": [
            "Web upstream is a scripted routing fixture, not an integrated API/identity service.",
            "Local generation, full Compose bootstrap and live browser workflows are not exercised.",
            "Embedding/reranking use already-prepared read-only local artifacts; first-install downloads are not tested.",
            "These newly built images need vulnerability/SBOM review; packaging success does not open the release gate.",
        ],
    }
    with args.output.open("x") as stream:
        stream.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"scope": report["scope"], "status": "pass", "output": str(args.output)}))


if __name__ == "__main__":
    main()

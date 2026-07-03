import asyncio
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import pytest
from evidencedesk.models.catalog import endpoint_sha256
from psycopg.types.json import Jsonb

from tests.integration.answers.test_execution import ANSWER
from tests.integration.answers.test_execution import grounded as grounded

pytestmark = pytest.mark.integration


async def test_actual_chat_worker_command_uses_read_only_index_and_scoped_native_provider(
    grounded,
    qdrant_server,
    tmp_path,
):
    calls = []
    model = grounded.binding.model

    class LocalProvider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.path == "/api/tags"
            body = json.dumps(
                {"models": [{"name": model.model_id, "digest": model.resolved_digest}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            assert self.path == "/api/chat"
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(value)
            body = (
                json.dumps(
                    {
                        "model": model.model_id,
                        "done": True,
                        "done_reason": "stop",
                        "message": {"content": json.dumps(ANSWER)},
                        "prompt_eval_count": 900,
                        "eval_count": 30,
                    }
                )
                + "\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = grounded.client.app.state.settings
    settings.local_model_url = f"http://127.0.0.1:{server.server_port}"
    with psycopg.connect(grounded.database["operator"]) as conn:
        conn.execute(
            "UPDATE app.provider_health SET capability_report=capability_report || %s WHERE model_key=%s",
            (Jsonb({"endpoint_sha256": endpoint_sha256(model, settings)}), model.key),
        )
    run = grounded.admit()
    key = tmp_path / "readonly-qdrant"
    key.write_text(qdrant_server[2])
    # A container-style working directory has no host .cache/models fallback.
    # All actual retrieval/reranking/tokenization must use the configured cache.
    root = Path.cwd()
    working = tmp_path / "worker-runtime"
    working.mkdir()
    (working / "contracts").symlink_to(root / "contracts", target_is_directory=True)
    model_cache = tmp_path / "configured-model-cache"
    model_cache.symlink_to(root / ".cache/models", target_is_directory=True)
    assert not (working / ".cache/models").exists()
    env = {k: v for k, v in os.environ.items() if not k.startswith("ED_")}
    env.update(
        ED_DATABASE_URL=grounded.database["ed_app"],
        ED_DISPATCH_DATABASE_URL=grounded.database["ed_dispatch"],
        ED_IDENTITY_DATABASE_URL=grounded.database["ed_identity"],
        ED_CHECKPOINT_DATABASE_URL=grounded.database["ed_checkpoint"],
        ED_QDRANT_URL=qdrant_server[0],
        ED_QDRANT_READ_KEY_FILE=str(key),
        ED_MODEL_CONFIG_PATH=str(settings.model_config_path),
        ED_LOCAL_MODEL_URL=settings.local_model_url,
        ED_MODEL_CACHE_PATH=str(model_cache),
        ED_MODEL_ARTIFACT_LOCK=str(root / "contracts/config/model-artifacts.lock.json"),
        ED_LOCAL_TOKENIZER_LOCK=str(root / "contracts/config/local-tokenizer.lock.json"),
    )
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [str(Path.cwd() / ".venv/bin/evidencedesk"), "chat-worker", "--once"],
            cwd=working,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert grounded.row(run)[0] == "completed", grounded.row(run)
        assert len(calls) == 1
        assert qdrant_server[2] not in result.stdout + result.stderr
        assert "Fixture evidence" not in result.stdout + result.stderr
        assert "ED_S3_SECRET_KEY_FILE" not in env and "ED_OPENAI_KEY_FILE" not in env
        assert "ED_SESSION_KEY_FILE" not in env and "ED_OIDC_CLIENT_SECRET_FILE" not in env
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)

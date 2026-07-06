import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.security.uploads.test_uploads import setup, upload

pytestmark = pytest.mark.integration


async def test_worker_processes_publish_and_audit_without_browser_or_paid_credentials(
    auth_client, object_store, database, writer, qdrant_server, tmp_path, parser_image
):
    client, _, _, h, base, url = setup(auth_client)
    accepted = upload(client, url, h, b"Persistent worker command integration.").json()
    access, secret, key = (tmp_path / name for name in ("access", "secret", "qdrant"))
    access.write_text("testing")
    secret.write_text("testing")
    key.write_text(qdrant_server[1])
    env = {k: v for k, v in os.environ.items() if not k.startswith("ED_")}
    env.update(
        ED_DATABASE_URL=database["ed_app"],
        ED_DISPATCH_DATABASE_URL=database["ed_dispatch"],
        ED_S3_ENDPOINT=object_store.client.meta.endpoint_url,
        ED_S3_BUCKET=object_store.bucket,
        ED_S3_ACCESS_KEY_FILE=str(access),
        ED_S3_SECRET_KEY_FILE=str(secret),
        ED_QDRANT_URL=qdrant_server[0],
        ED_QDRANT_WRITE_KEY_FILE=str(key),
    )

    async def invoke(*args):
        result = await asyncio.to_thread(
            subprocess.run,
            [str(Path.cwd() / ".venv/bin/evidencedesk"), *args],
            env=env,
            timeout=60,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert qdrant_server[1] not in result.stdout + result.stderr
        return result

    await invoke("ingest-worker", "--once")
    assert (
        client.get(base + "/documents/" + accepted["document"]["id"]).json()["active_revision_id"]
        is None
    )
    await invoke("index-worker", "--once")
    await invoke("ingest-worker", "--once")
    assert (
        client.get(base + "/documents/" + accepted["document"]["id"]).json()["active_revision_id"]
        == accepted["revision_id"]
    )
    assert client.get(base + "/jobs/" + accepted["job"]["job_id"]).json()["status"] == "succeeded"
    workspace = accepted["document"]["workspace_id"]
    report = await invoke("index-reconcile", "--workspace", workspace)
    assert json.loads(report.stdout)["checked"] == 1
    report = await invoke("index-reconcile", "--workspace", workspace, "--orphans")
    assert json.loads(report.stdout)["removed"] == 0

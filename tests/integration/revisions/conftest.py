import secrets
import subprocess
import time
from uuid import uuid4

import httpx
import pytest
from evidencedesk.index.qdrant import IndexConfig, IndexWriter, client

from tests.integration.embeddings.test_encoder import encoder as encoder
from tests.integration.ingestion.test_sandbox import parser_image as parser_image

QD_IMAGE = "qdrant/qdrant@sha256:b7b0444c4c351c970b98e90a6f89c2ee4287c65b44e52b4cb503fa5b2aa927ad"


@pytest.fixture(scope="session")
def qdrant_server():
    name = "ed-index-test-" + uuid4().hex[:12]
    write_key, read_key = secrets.token_hex(24), secrets.token_hex(24)
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "--memory",
            "1g",
            "--cpus",
            "2",
            "-p",
            "127.0.0.1::6333",
            "-e",
            f"QDRANT__SERVICE__API_KEY={write_key}",
            "-e",
            f"QDRANT__SERVICE__READ_ONLY_API_KEY={read_key}",
            QD_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    try:
        port = (
            subprocess.check_output(["docker", "port", name, "6333/tcp"], text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        origin = f"http://127.0.0.1:{port}"
        with httpx.Client(trust_env=False) as http:
            for _ in range(100):
                try:
                    if http.get(origin + "/readyz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Disposable Qdrant did not start")
        yield origin, write_key, read_key
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
async def writer(qdrant_server):
    origin, key, _ = qdrant_server
    connection = client(origin, key)
    writer = IndexWriter(connection, IndexConfig())
    try:
        await writer.provision()
        yield writer
    finally:
        if await connection.collection_exists(writer.config.collection):
            await connection.delete_collection(writer.config.collection)
        await connection.close()

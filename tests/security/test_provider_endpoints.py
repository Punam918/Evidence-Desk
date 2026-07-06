import json

import httpx
import pytest
from evidencedesk.providers.base import ModelFault
from evidencedesk.providers.ollama import OllamaAdapter

from tests.contracts.providers.test_ollama import DIGEST, MODEL, REQUEST, Counter


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://169.254.169.254",
        "https://169.254.169.254",
        "https://[::ffff:169.254.169.254]",
        "https://[::ffff:0.0.0.0]",
        "https://[fe80::1%25eth0]",
        "https://0.0.0.0",
        "https://224.0.0.1",
        "https://2852039166",
        "https://0xa9fea9fe",
        "https://0177.0.0.1",
        "https://:secret@provider.example",
        "https://@provider.example",
        "https://provider.example:0",
        "https://provider.example:65536",
        "https://provider.example\n",
        "https://provider.\nexample",
        "https://provider.example\\@127.0.0.1",
        "https://provider.example/?token=secret",
        "http://provider.example",
        "file:///etc/shadow",
    ],
)
def test_ambiguous_or_metadata_origins_are_rejected_before_transport(endpoint):
    with pytest.raises(ValueError):
        OllamaAdapter(endpoint, MODEL, DIGEST, Counter())


async def test_redirect_cannot_move_local_generation_to_metadata(monkeypatch):
    seen = []
    monkeypatch.setenv("HTTPS_PROXY", "http://169.254.169.254:8080")
    monkeypatch.setenv("OLLAMA_HOST", "http://169.254.169.254")

    def handle(request):
        seen.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": MODEL, "digest": DIGEST}]})
        return httpx.Response(307, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    adapter = OllamaAdapter(
        "http://127.0.0.1:11435", MODEL, DIGEST, Counter(), transport=httpx.MockTransport(handle)
    )
    try:
        result = [event async for event in adapter.stream(REQUEST)]
        assert isinstance(result[-1], ModelFault)
        assert len(seen) == 2 and all(request.url.host == "127.0.0.1" for request in seen)
        assert "tools" not in json.loads(seen[-1].content)
    finally:
        await adapter.close()

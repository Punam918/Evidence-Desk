import json
from pathlib import Path

import httpx2
import pytest
from evidencedesk.models.registry import load_registry
from evidencedesk.providers.openai import OpenAIAdapter
from evidencedesk.providers.probe import probe, probe_reservation

from tests.contracts.providers.test_openai import ANSWER, Bytes, completed, delta


@pytest.mark.parametrize("failure", [None, "schema", "usage", "stream"])
async def test_native_probe_checks_capabilities_and_closes_cancelled_stream(failure):
    model = next(
        m for m in load_registry(Path("contracts/config/models.yaml")) if m.key == "paid_standard"
    )
    wires = []
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert body["max_output_tokens"] == 160
        raw = json.dumps(ANSWER) if failure != "schema" else "not JSON"
        events = [completed(raw, usage=failure != "usage")]
        if failure != "stream":
            events.insert(0, delta(raw))
        wire = Bytes("".join("data: " + json.dumps(e) + "\n\n" for e in events).encode())
        wires.append(wire)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    adapter = OpenAIAdapter("fixture-secret", transport=httpx2.MockTransport(handler))
    try:
        assert 0 < probe_reservation(adapter, model) < 10_000
        result = await probe(adapter, model)
        assert all(w.closed for w in wires)
        if failure is None:
            assert result.schema and result.streaming and result.cancellation and result.usage
            assert len(bodies) == 2
        else:
            assert not result.cancellation
            assert len(bodies) == 1
            assert not getattr(result, "streaming" if failure == "stream" else failure)
    finally:
        await adapter.close()

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from evidencedesk.providers.base import ModelFault, ModelRequest, ModelResult, TextDelta, TokenCount
from evidencedesk.providers.local_tokens import LocalTokens
from evidencedesk.providers.ollama import OllamaAdapter

from tests.contracts.providers.test_openai import ANSWER

DIGEST = "a" * 64
MODEL = "evidencedesk-qwen3-8b:fixture"
REQUEST = ModelRequest(
    "local_standard",
    MODEL,
    "Only cite evidence.",
    "What?",
    context="E1: Café नेपाल 😀.",
    context_cap=8192,
)


class Counter:
    def count(self, messages, schema):
        return TokenCount(500, True, "fixture")


class Bytes(httpx.AsyncByteStream):
    def __init__(self, payload):
        self.payload, self.closed = payload, False

    async def __aiter__(self):
        for byte in self.payload:
            yield bytes([byte])

    async def aclose(self):
        self.closed = True


def chunk(content="", done=False, **kwargs):
    return {
        "model": MODEL,
        "message": {"role": "assistant", "content": content},
        "done": done,
        **kwargs,
    }


async def collect(events, *, digest=DIGEST, status=200):
    seen = []
    wire = Bytes("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events).encode())

    def handler(request):
        seen.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": MODEL, "digest": digest}]})
        return httpx.Response(status, stream=wire)

    adapter = OllamaAdapter(
        "http://127.0.0.1:11435", MODEL, DIGEST, Counter(), transport=httpx.MockTransport(handler)
    )
    try:
        result = [event async for event in adapter.stream(REQUEST)]
    finally:
        await adapter.close()
    return result, seen, wire


async def test_native_local_unicode_schema_usage_and_private_only():
    raw = json.dumps(ANSWER, ensure_ascii=False)
    result, seen, wire = await collect(
        [
            chunk(raw[:30]),
            chunk(raw[30:]),
            chunk(done=True, done_reason="stop", prompt_eval_count=100, eval_count=20),
            chunk(done=True),
        ]
    )
    assert "".join(event.text for event in result if isinstance(event, TextDelta)) == raw
    assert (
        isinstance(result[-1], ModelResult)
        and result[-1].answer.sentences[0].text == "Café नेपाल 😀."
    )
    assert result[-1].usage.input_tokens == 100 and result[-1].usage.output_tokens == 20
    assert (
        all(request.url.host == "127.0.0.1" for request in seen) and len(seen) == 2 and wire.closed
    )
    body = json.loads(seen[-1].content)
    assert (
        body["think"] is False
        and body["options"]["num_ctx"] == 8192
        and body["options"]["num_predict"] == 1024
        and "tools" not in body
    )


async def test_digest_mismatch_never_sends_a_prompt():
    result, seen, _ = await collect([], digest="b" * 64)
    assert result == [ModelFault("local_model_digest_mismatch", "definite_failure")]
    assert [request.url.path for request in seen] == ["/api/tags"]


@pytest.mark.parametrize(
    "events,expected",
    [
        ([chunk("partial")], "provider_missing_terminal"),
        (
            [chunk(message={"role": "assistant", "thinking": "private reasoning", "content": ""})],
            "local_conformance_failed",
        ),
        (
            [
                chunk(
                    message={"role": "assistant", "tool_calls": [{"function": {"name": "unsafe"}}]}
                )
            ],
            "local_conformance_failed",
        ),
        ([chunk("partial"), {"error": "SECRET OOM details"}], "local_generation_failed"),
        ([chunk(model="foreign")], "local_conformance_failed"),
    ],
)
async def test_interrupted_thinking_tools_and_oom_fail_closed(events, expected):
    result, _, _ = await collect(events)
    assert result[-1].code == expected and result[-1].execution == "ambiguous"
    assert "SECRET" not in repr(result)


@pytest.mark.parametrize(
    "reason,raw,status",
    [
        ("length", "partial", "incomplete"),
        ("stop", "", "invalid"),
        ("stop", '{"status":"answered","sentences":[],"limitations":[]}', "invalid"),
    ],
)
async def test_local_terminal_semantics(reason, raw, status):
    result, _, _ = await collect([chunk(raw, done=True, done_reason=reason)])
    assert result[-1].status == status and result[-1].usage is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com",
        "https://169.254.169.254",
        "https://user:secret@host",
        "https://host/path",
        "https://host?url=evil",
    ],
)
def test_arbitrary_or_unsafe_endpoint_is_rejected(endpoint):
    with pytest.raises(ValueError):
        OllamaAdapter(endpoint, MODEL, DIGEST, Counter())


async def test_local_endpoint_down_never_calls_hosted():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("fixture", request=request)

    adapter = OllamaAdapter(
        "http://127.0.0.1:11435", MODEL, DIGEST, Counter(), transport=httpx.MockTransport(handler)
    )
    try:
        result = [event async for event in adapter.stream(REQUEST)]
        assert result == [ModelFault("provider_transport_interrupted", "definite_failure")]
        assert len(calls) == 1 and calls[0].url.host == "127.0.0.1"
    finally:
        await adapter.close()


async def test_local_cold_first_event_deadline_is_separate_from_idle_deadline():
    class SlowFirstByte(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            await asyncio.sleep(0.04)
            yield (json.dumps(chunk("draft")) + "\n").encode()

        async def aclose(self):
            self.closed = True

    wire = SlowFirstByte()

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": MODEL, "digest": DIGEST}]})
        return httpx.Response(200, stream=wire)

    adapter = OllamaAdapter(
        "http://127.0.0.1:11435",
        MODEL,
        DIGEST,
        Counter(),
        transport=httpx.MockTransport(handler),
        first_event_timeout=0.01,
        idle_timeout=0.005,
        total_timeout=1,
    )
    try:
        result = [event async for event in adapter.stream(REQUEST)]
    finally:
        await adapter.close()
    assert result == [ModelFault("provider_transport_interrupted", "ambiguous")]
    assert wire.closed


async def test_cancel_closes_local_stream():
    entered = asyncio.Event()

    class Pending(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield (json.dumps(chunk("draft")) + "\n").encode()
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    pending = Pending()

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": MODEL, "digest": DIGEST}]})
        return httpx.Response(200, stream=pending)

    adapter = OllamaAdapter(
        "http://127.0.0.1:11435", MODEL, DIGEST, Counter(), transport=httpx.MockTransport(handler)
    )

    async def consume():
        return [event async for event in adapter.stream(REQUEST)]

    task = asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await adapter.close()
    assert pending.closed


@pytest.mark.models
def test_pinned_qwen_template_and_schema_token_estimate():
    tokenizer = LocalTokens(
        Path(".cache/models/qwen3-tokenizer"), Path("contracts/config/local-tokenizer.lock.json")
    )
    estimate = tokenizer.count(
        [
            {"role": "system", "content": "Only evidence."},
            {"role": "user", "content": "What is documented?"},
        ],
        {"type": "object"},
    )
    assert estimate.estimated and estimate.tokens > 20
    assert "<think>\n\n</think>" in tokenizer.template.render(
        messages=[{"role": "user", "content": "Question"}],
        tools=None,
        add_generation_prompt=True,
        enable_thinking=False,
    )

import asyncio
import json
from decimal import Decimal

import httpx2
import pytest
from evidencedesk.providers.base import (
    ModelFault,
    ModelRequest,
    ModelResult,
    Prices,
    TextDelta,
    Usage,
)
from evidencedesk.providers.openai import OpenAIAdapter

ANSWER = {
    "status": "answered",
    "sentences": [{"text": "Café नेपाल 😀.", "citation_ids": ["E1"]}],
    "limitations": [],
}
REQUEST = ModelRequest(
    "paid_standard", "fixture-model", "Use only cited evidence.", "What?", "E1: Café नेपाल 😀."
)


def completed(raw, *, kind="response.completed", usage=True, refusal=False):
    content = (
        [{"type": "refusal", "refusal": "No."}]
        if refusal
        else [{"type": "output_text", "text": raw, "annotations": []}]
    )
    return {
        "type": kind,
        "sequence_number": 5,
        "response": {
            "id": "resp_fixture",
            "object": "response",
            "created_at": 1,
            "model": "fixture-resolved",
            "status": "completed" if kind == "response.completed" else "incomplete",
            "output": [
                {
                    "type": "message",
                    "id": "msg_fixture",
                    "role": "assistant",
                    "status": "completed",
                    "content": content,
                }
            ],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_tokens_details": {"cached_tokens": 30},
                "output_tokens_details": {"reasoning_tokens": 5},
            }
            if usage
            else None,
        },
    }


def delta(raw):
    return {
        "type": "response.output_text.delta",
        "delta": raw,
        "sequence_number": 2,
        "output_index": 0,
        "content_index": 0,
        "item_id": "msg_fixture",
        "logprobs": [],
    }


class Bytes(httpx2.AsyncByteStream):
    def __init__(self, payload, step=1):
        self.payload, self.step, self.closed = payload, step, False

    async def __aiter__(self):
        for start in range(0, len(self.payload), self.step):
            yield self.payload[start : start + self.step]

    async def aclose(self):
        self.closed = True


async def collect(events, *, status=200):
    requests = []
    wire = Bytes(
        "".join(
            "data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events
        ).encode()
    )

    def handler(request):
        requests.append(request)
        if status != 200:
            return httpx2.Response(
                status,
                json={"error": {"code": "fixture", "message": "SECRET must never escape"}},
                headers={"Retry-After": "2"},
            )
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    adapter = OpenAIAdapter("fixture-secret", transport=httpx2.MockTransport(handler))
    try:
        result = [e async for e in adapter.stream(REQUEST)]
    finally:
        await adapter.close()
    return result, requests, wire


async def test_native_stream_unicode_usage_and_no_hidden_retries():
    raw = json.dumps(ANSWER, ensure_ascii=False)
    result, requests, wire = await collect(
        [delta(raw[:20]), delta(raw[20:]), completed(raw), completed(raw)]
    )
    assert "".join(e.text for e in result if isinstance(e, TextDelta)) == raw
    assert (
        isinstance(result[-1], ModelResult)
        and result[-1].answer.sentences[0].text == "Café नेपाल 😀."
    )
    assert result[-1].usage == Usage(
        input_tokens=100, output_tokens=20, cached_input_tokens=30, reasoning_output_tokens=5
    )
    assert (
        len([e for e in result if isinstance(e, ModelResult)]) == 1
        and len(requests) == 1
        and wire.closed
    )
    body = json.loads(requests[0].content)
    assert (
        body["store"] is False
        and body["truncation"] == "disabled"
        and body["text"]["format"]["strict"]
    )
    assert "tools" not in body and "previous_response_id" not in body


@pytest.mark.parametrize(
    "kind,refusal,expected",
    [
        ("response.completed", True, "refused"),
        ("response.incomplete", False, "incomplete"),
        ("response.completed", False, "invalid"),
    ],
)
async def test_refusal_incomplete_and_invalid_local_schema(kind, refusal, expected):
    result, _, _ = await collect(
        [
            completed(
                '{"status":"answered","sentences":[],"limitations":[]}', kind=kind, refusal=refusal
            )
        ]
    )
    assert result[-1].status == expected and result[-1].answer is None


@pytest.mark.parametrize(
    "status,execution,retryable",
    [
        (429, "definite_failure", True),
        (503, "ambiguous", False),
        (401, "definite_failure", False),
        (400, "definite_failure", False),
    ],
)
async def test_http_errors_are_safe_and_exactly_one_attempt(status, execution, retryable):
    result, requests, _ = await collect([], status=status)
    assert len(requests) == 1 and len(result) == 1 and isinstance(result[0], ModelFault)
    assert (result[0].execution, result[0].retryable) == (execution, retryable)
    assert "SECRET" not in repr(result)


async def test_partial_without_terminal_is_ambiguous():
    result, _, _ = await collect([delta("partial")])
    assert result == [TextDelta("partial"), ModelFault("provider_missing_terminal", "ambiguous")]


async def test_missing_usage_is_explicit_and_terminal_text_mismatch_fails():
    raw = json.dumps(ANSWER)
    result, _, _ = await collect([completed(raw, usage=False)])
    assert result[-1].usage is None
    result, _, _ = await collect([delta("different"), completed(raw)])
    assert result[-1] == ModelFault("provider_text_mismatch", "ambiguous")


async def test_cancellation_closes_native_transport():
    entered = asyncio.Event()

    class Pending(httpx2.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield ("data: " + json.dumps(delta("draft")) + "\n\n").encode()
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    pending = Pending()
    adapter = OpenAIAdapter(
        "fixture",
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=pending
            )
        ),
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


def test_prices_use_inclusive_subsets_and_round_once():
    prices = Prices("fixture", Decimal(".1"), Decimal(".5"), Decimal(".05"))
    assert (
        prices.charge(
            Usage(
                input_tokens=100,
                output_tokens=20,
                cached_input_tokens=30,
                reasoning_output_tokens=5,
            )
        )
        == 19
    )
    with pytest.raises(ValueError):
        Usage(input_tokens=1, output_tokens=1, cached_input_tokens=2)


async def test_complete_request_estimate_and_native_count_include_schema_history():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json={"object": "response.input_tokens", "input_tokens": 123})

    adapter = OpenAIAdapter("fixture", transport=httpx2.MockTransport(handler))
    try:
        estimate = adapter.estimate(REQUEST)
        exact = await adapter.count(REQUEST)
        assert estimate.estimated and estimate.tokens > 100
        assert not exact.estimated and exact.tokens == 123
        assert seen[0]["text"]["format"]["schema"]["properties"]["sentences"]
    finally:
        await adapter.close()


async def test_error_after_draft_and_unexpected_tool_fail_without_retry():
    result, requests, _ = await collect(
        [
            delta("partial"),
            {
                "type": "error",
                "code": "fixture",
                "message": "SECRET",
                "param": None,
                "sequence_number": 3,
            },
        ]
    )
    assert result[-1] == ModelFault("provider_failed", "ambiguous") and len(requests) == 1
    tool = {
        "type": "response.output_item.added",
        "sequence_number": 1,
        "output_index": 0,
        "item": {
            "type": "function_call",
            "id": "fc_fixture",
            "call_id": "call_fixture",
            "name": "forbidden",
            "arguments": "{",
        },
    }
    result, _, _ = await collect([tool])
    assert result == [ModelFault("unexpected_provider_tool", "ambiguous")]


async def test_transport_connect_failure_is_not_blindly_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx2.ConnectError("SECRET endpoint details", request=request)

    adapter = OpenAIAdapter("fixture", transport=httpx2.MockTransport(handler))
    try:
        result = [event async for event in adapter.stream(REQUEST)]
        assert (
            result == [ModelFault("provider_transport_interrupted", "ambiguous")]
            and len(calls) == 1
        )
    finally:
        await adapter.close()


async def test_context_overflow_and_empty_completion():
    adapter = OpenAIAdapter(
        "fixture",
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(
                400, json={"error": {"code": "context_length_exceeded", "message": "too large"}}
            )
        ),
    )
    try:
        result = [event async for event in adapter.stream(REQUEST)]
        assert (
            result[0].code == "provider_context_overflow"
            and result[0].execution == "definite_failure"
        )
    finally:
        await adapter.close()
    result, _, _ = await collect([completed("")])
    assert result[-1].status == "invalid"


async def test_idle_timeout_is_ambiguous_and_closes_stream():
    class TimedOut(httpx2.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield ("data: " + json.dumps(delta("partial")) + "\n\n").encode()
            raise httpx2.ReadTimeout("SECRET timeout")

        async def aclose(self):
            self.closed = True

    body = TimedOut()
    adapter = OpenAIAdapter(
        "fixture",
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=body
            )
        ),
    )
    try:
        result = [event async for event in adapter.stream(REQUEST)]
        assert (
            result[-1] == ModelFault("provider_transport_interrupted", "ambiguous") and body.closed
        )
    finally:
        await adapter.close()


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("rewrite", {"query": "What about Lumen archives?"}),
        ("summary", {"facts": [], "open_questions": ["Which archive?"]}),
    ],
)
async def test_native_auxiliary_schema_is_distinct_and_validated(kind, payload):
    from dataclasses import replace

    from evidencedesk.providers.schema import request_fingerprint

    raw = json.dumps(payload)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        wire = Bytes(
            "".join(
                "data: " + json.dumps(e) + "\n\n" for e in [delta(raw), completed(raw)]
            ).encode()
        )
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    adapter = OpenAIAdapter("fixture-secret", transport=httpx2.MockTransport(handler))
    try:
        request = replace(
            REQUEST, output_kind=kind, max_output_tokens=256 if kind == "rewrite" else 384
        )
        events = [e async for e in adapter.stream(request)]
        assert events[-1].status == "completed" and events[-1].answer is None
        assert events[-1].raw_text == raw
        assert requests[0]["text"]["format"]["name"] == "memory_" + kind
        assert request_fingerprint(request) != request_fingerprint(REQUEST)
    finally:
        await adapter.close()


def test_answer_fingerprint_preserves_pre_auxiliary_frozen_requests():
    import hashlib
    from dataclasses import asdict

    from evidencedesk.identity.crypto import canonical_json
    from evidencedesk.providers.schema import answer_format, request_fingerprint

    legacy = asdict(REQUEST)
    legacy.pop("output_kind")
    expected = hashlib.sha256(
        canonical_json({"request": legacy, "answer_schema": answer_format()}).encode()
    ).hexdigest()
    assert request_fingerprint(REQUEST) == expected

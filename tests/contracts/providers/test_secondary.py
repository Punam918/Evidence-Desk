import json
from dataclasses import replace

import httpx2
import pytest
from evidencedesk.providers.anthropic import AnthropicAdapter
from evidencedesk.providers.base import ModelFault, ModelRequest, ModelResult, TextDelta
from evidencedesk.providers.google import GoogleAdapter
from evidencedesk.providers.secondary_schema import compile_schema, compile_subset

from tests.contracts.providers.test_openai import ANSWER, Bytes

REQUEST = ModelRequest(
    "secondary", "fixture-model", "Use only supplied evidence.", "What?", "E1: Café नेपाल 😀."
)


def anthropic_events(raw, *, reason="end_turn", usage=True):
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "model": "fixture-resolved",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 70,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": raw[:20]},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": raw[20:]},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": reason, "stop_sequence": None},
            "usage": {"output_tokens": 20, "output_tokens_details": {"thinking_tokens": 5}}
            if usage
            else {},
        },
        {"type": "message_stop"},
    ]


def google_events(raw, *, reason="STOP", usage=True):
    return [
        {
            "modelVersion": "fixture-resolved",
            "responseId": "response-fixture",
            "candidates": [
                {"index": 0, "content": {"role": "model", "parts": [{"text": raw[:20]}]}}
            ],
        },
        {
            "modelVersion": "fixture-resolved",
            "responseId": "response-fixture",
            "candidates": [
                {
                    "index": 0,
                    "content": {"role": "model", "parts": [{"text": raw[20:]}]},
                    "finishReason": reason,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "cachedContentTokenCount": 30,
                "candidatesTokenCount": 15,
                "thoughtsTokenCount": 5,
                "totalTokenCount": 120,
            }
            if usage
            else None,
        },
    ]


async def collect(provider, events, *, status=200, cancel=False, request=REQUEST):
    requests = []
    payload = "".join(
        ("event: " + event["type"] + "\n" if provider == "anthropic" else "")
        + "data: "
        + json.dumps(event, ensure_ascii=False)
        + "\n\n"
        for event in events
    ).encode()
    wire = Bytes(payload, step=3)

    def handler(request):
        requests.append(request)
        if status != 200:
            return httpx2.Response(
                status,
                json={
                    "error": {
                        "type": "rate_limit_error",
                        "code": status,
                        "status": "RESOURCE_EXHAUSTED",
                        "message": "SECRET provider details",
                    }
                },
                headers={"Retry-After": "2"},
            )
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    adapter = (AnthropicAdapter if provider == "anthropic" else GoogleAdapter)(
        "fixture-secret", transport=httpx2.MockTransport(handler)
    )
    output = []
    stream = adapter.stream(request)
    try:
        async for event in stream:
            output.append(event)
            if cancel and isinstance(event, TextDelta):
                break
    finally:
        await stream.aclose()
        await adapter.close()
    return output, requests, wire


@pytest.mark.parametrize("provider", ["anthropic", "google"])
async def test_native_secondary_schema_unicode_inclusive_usage_and_fixed_origin(provider):
    raw = json.dumps(ANSWER, ensure_ascii=False)
    events = (anthropic_events if provider == "anthropic" else google_events)(raw)
    result, requests, wire = await collect(provider, events)
    assert isinstance(result[-1], ModelResult), result
    assert (
        result[-1].status == "completed"
        and result[-1].answer.sentences[0].text == ANSWER["sentences"][0]["text"]
    )
    assert "".join(event.text for event in result if isinstance(event, TextDelta)) == raw
    assert result[-1].usage.input_tokens == 100 and result[-1].usage.output_tokens == 20
    assert (
        result[-1].usage.cached_input_tokens == 30 and result[-1].usage.reasoning_output_tokens == 5
    )
    assert len(requests) == 1 and wire.closed
    assert requests[0].url.host == (
        "api.anthropic.com" if provider == "anthropic" else "generativelanguage.googleapis.com"
    )
    body = json.loads(requests[0].content)
    assert "tools" not in body
    if provider == "anthropic":
        assert body["thinking"] == {"type": "disabled"}
        assert body["output_config"]["format"]["type"] == "json_schema"
    else:
        assert body["generationConfig"]["candidateCount"] == 1
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert "tools" not in body


@pytest.mark.parametrize("provider", ["anthropic", "google"])
@pytest.mark.parametrize("status", [400, 401, 429, 503])
async def test_secondary_faults_do_not_retry_or_expose_upstream_errors(provider, status):
    result, requests, _ = await collect(provider, [], status=status)
    assert len(requests) == 1
    assert isinstance(result[-1], ModelFault), result
    assert result[-1].execution == ("ambiguous" if status == 503 else "definite_failure")
    assert result[-1].retryable == (status == 429)
    assert "SECRET" not in repr(result)


@pytest.mark.parametrize("provider", ["anthropic", "google"])
@pytest.mark.parametrize("terminal", ["refused", "incomplete", "invalid", "missing"])
async def test_secondary_terminal_mapping_without_refusal_fallback(provider, terminal):
    raw = json.dumps(ANSWER) if terminal != "invalid" else "not JSON"
    if provider == "anthropic":
        events = anthropic_events(
            raw, reason={"refused": "refusal", "incomplete": "max_tokens"}.get(terminal, "end_turn")
        )
        if terminal == "missing":
            events = events[:-1]
    else:
        events = google_events(
            raw, reason={"refused": "SAFETY", "incomplete": "MAX_TOKENS"}.get(terminal, "STOP")
        )
        if terminal == "missing":
            events = events[:1]
    result, _, wire = await collect(provider, events)
    assert wire.closed
    if terminal == "missing":
        assert isinstance(result[-1], ModelFault) and result[-1].execution == "ambiguous"
    else:
        assert isinstance(result[-1], ModelResult), result
        assert result[-1].status == terminal


@pytest.mark.parametrize("provider", ["anthropic", "google"])
async def test_secondary_cancel_closes_native_stream_without_terminal(provider):
    events = (anthropic_events if provider == "anthropic" else google_events)(json.dumps(ANSWER))
    result, requests, wire = await collect(provider, events, cancel=True)
    assert len(requests) == 1 and wire.closed
    assert result and all(isinstance(event, TextDelta) for event in result)


@pytest.mark.parametrize("provider", ["anthropic", "google"])
@pytest.mark.parametrize(
    "kind,payload",
    [("rewrite", {"query": "What about Lumen?"}), ("summary", {"facts": [], "open_questions": []})],
)
async def test_secondary_auxiliary_schemas_are_locally_validated(provider, kind, payload):
    request = replace(
        REQUEST, output_kind=kind, max_output_tokens=256 if kind == "rewrite" else 384
    )
    events = (anthropic_events if provider == "anthropic" else google_events)(json.dumps(payload))
    result, _, _ = await collect(provider, events, request=request)
    assert (
        isinstance(result[-1], ModelResult)
        and result[-1].status == "completed"
        and result[-1].answer is None
    )


def test_schema_subset_transformations_are_explicit_and_unknown_keywords_fail():
    for provider in ("anthropic", "google"):
        compiled = compile_schema("answer", provider)
        assert compiled.local_constraints and "$defs" not in compiled.schema
        assert "Local validation requires" in json.dumps(compiled.schema)
        for schema in (
            {"type": "string", "unknownKeyword": True},
            {"$ref": "https://evil.invalid/schema"},
            {"$defs": {"Loop": {"$ref": "#/$defs/Loop"}}, "$ref": "#/$defs/Loop"},
        ):
            with pytest.raises(ValueError):
                compile_subset(schema, provider)


@pytest.mark.parametrize("provider", ["anthropic", "google"])
async def test_secondary_missing_usage_is_unknown_not_zero(provider):
    events = (anthropic_events if provider == "anthropic" else google_events)(
        json.dumps(ANSWER), usage=False
    )
    result, _, _ = await collect(provider, events)
    assert isinstance(result[-1], ModelResult) and result[-1].status == "completed"
    assert result[-1].usage is None


@pytest.mark.parametrize("provider", ["anthropic", "google"])
async def test_secondary_hidden_thought_text_is_not_emitted_and_usage_is_not_duplicated(provider):
    raw = json.dumps(ANSWER)
    if provider == "anthropic":
        events = anthropic_events(raw)
        thought = [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "PRIVATE_THOUGHT"},
            },
            {"type": "content_block_stop", "index": 0},
        ]
        for event in events[1:-2]:
            event["index"] += 1
        events = events[:1] + thought + events[1:-1] + [events[-2], events[-1]]
    else:
        events = google_events(raw)
        events[0]["candidates"][0]["content"]["parts"].insert(
            0, {"text": "PRIVATE_THOUGHT", "thought": True}
        )
        events.append({"usageMetadata": events[-1]["usageMetadata"]})
    result, _, _ = await collect(provider, events)
    assert isinstance(result[-1], ModelResult), result
    assert result[-1].usage.output_tokens == 20
    assert "PRIVATE_THOUGHT" not in repr(result)


@pytest.mark.parametrize("provider", ["anthropic", "google"])
async def test_secondary_tools_never_execute_or_become_answer_text(provider):
    events = (anthropic_events if provider == "anthropic" else google_events)(json.dumps(ANSWER))
    if provider == "anthropic":
        events[1]["content_block"] = {
            "type": "tool_use",
            "id": "tool_fixture",
            "name": "unsafe",
            "input": {},
        }
    else:
        events[0]["candidates"][0]["content"]["parts"] = [
            {"functionCall": {"name": "unsafe", "args": {}}}
        ]
    result, requests, wire = await collect(provider, events)
    assert isinstance(result[-1], ModelFault) and result[-1].code == "unexpected_provider_tool", (
        result
    )
    assert len(requests) == 1 and wire.closed


async def test_anthropic_cache_creation_fee_is_not_silently_priced_as_ordinary_input():
    events = anthropic_events(json.dumps(ANSWER))
    events[0]["message"]["usage"]["cache_creation_input_tokens"] = 10
    result, _, _ = await collect("anthropic", events)
    assert result[-1] == ModelFault("unsupported_cache_write_charge", "ambiguous")


async def test_google_empty_and_prompt_blocked_responses_are_distinct():
    empty, _, _ = await collect("google", [{"candidates": []}])
    assert isinstance(empty[-1], ModelFault)
    blocked, _, _ = await collect("google", [{"promptFeedback": {"blockReason": "SAFETY"}}])
    assert isinstance(blocked[-1], ModelResult) and blocked[-1].status == "refused"


def test_google_model_url_cannot_override_fixed_endpoint():
    with pytest.raises(ValueError, match="alias"):
        GoogleAdapter.body(replace(REQUEST, model_id="https://evil.invalid/model"))


@pytest.mark.parametrize("provider", ["anthropic", "google"])
async def test_secondary_native_count_includes_schema_and_instructions_and_is_labelled_estimated(
    provider,
):
    observed = []

    def handler(request):
        observed.append(json.loads(request.content))
        return httpx2.Response(
            200, json={"input_tokens": 512} if provider == "anthropic" else {"totalTokens": 512}
        )

    adapter = (AnthropicAdapter if provider == "anthropic" else GoogleAdapter)(
        "fixture-secret", transport=httpx2.MockTransport(handler)
    )
    try:
        count = await adapter.count(REQUEST)
        assert count.estimated and count.tokens >= 512
        assert len(observed) == 1
        body = json.dumps(observed[0])
        assert "supplied evidence" in body
        assert "sentences" in body and "citation_ids" in body
    finally:
        await adapter.close()


@pytest.mark.parametrize("provider", ["anthropic", "google"])
async def test_secondary_locally_enforces_constraints_omitted_by_remote_schema(provider):
    payload = {**ANSWER, "sentences": [{"text": "x" * 2001, "citation_ids": ["E1"]}]}
    events = (anthropic_events if provider == "anthropic" else google_events)(json.dumps(payload))
    result, _, _ = await collect(provider, events)
    assert isinstance(result[-1], ModelResult) and result[-1].status == "invalid"


async def test_anthropic_error_event_is_sanitized_and_ambiguous():
    events = anthropic_events(json.dumps(ANSWER))[:3] + [
        {
            "type": "error",
            "error": {"type": "overloaded_error", "message": "PRIVATE_UPSTREAM_SECRET"},
        }
    ]
    result, requests, wire = await collect("anthropic", events)
    assert isinstance(result[-1], ModelFault) and result[-1].execution == "ambiguous"
    assert "PRIVATE_UPSTREAM_SECRET" not in repr(result)
    assert len(requests) == 1 and wire.closed


async def test_google_interleaved_streams_keep_response_ownership_private():
    wires = []

    def handler(request):
        payload = "".join(
            "data: " + json.dumps(event) + "\n\n" for event in google_events(json.dumps(ANSWER))
        ).encode()
        wire = Bytes(payload, step=3)
        wires.append(wire)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    adapter = GoogleAdapter("fixture", transport=httpx2.MockTransport(handler))
    first, second = adapter.stream(REQUEST), adapter.stream(REQUEST)
    try:
        assert isinstance(await anext(first), TextDelta)
        assert adapter.responses.get() is None
        assert isinstance(await anext(second), TextDelta)
        assert adapter.responses.get() is None
        await first.aclose()
        assert wires[0].closed and not wires[1].closed
        remaining = [event async for event in second]
        assert isinstance(remaining[-1], ModelResult) and remaining[-1].status == "completed"
        assert wires[1].closed and adapter.responses.get() is None
    finally:
        await first.aclose()
        await second.aclose()
        await adapter.close()

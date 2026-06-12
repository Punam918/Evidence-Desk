"""Native Messages transport, explicit schema subset, no SDK retries or native tools."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, Literal

import httpx2
from anthropic import APIConnectionError, APIError, APIStatusError, AsyncAnthropic
from evidencedesk.providers.base import (
    ModelEvent,
    ModelFault,
    ModelRequest,
    ModelResult,
    TextDelta,
    TokenCount,
    Usage,
)
from evidencedesk.providers.schema import parse_output
from evidencedesk.providers.secondary_common import estimate_body, http_fault
from evidencedesk.providers.secondary_schema import compile_schema


class AnthropicAdapter:
    def __init__(self, api_key: str, *, transport: httpx2.AsyncBaseTransport | None = None):
        self.client = AsyncAnthropic(
            api_key=api_key,
            base_url="https://api.anthropic.com",
            max_retries=0,
            timeout=httpx2.Timeout(10, connect=3),
            http_client=httpx2.AsyncClient(
                transport=transport, trust_env=False, follow_redirects=False
            ),
        )

    async def close(self) -> None:
        await self.client.close()

    @staticmethod
    def body(request: ModelRequest) -> dict[str, Any]:
        messages = [{"role": role, "content": content} for role, content in request.history]
        messages.append(
            {
                "role": "user",
                "content": f"<evidence>\n{request.context}\n</evidence>\n<question>\n{request.query}\n</question>",
            }
        )
        return {
            "model": request.model_id,
            "system": request.instructions,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "service_tier": "standard_only",
            "thinking": {"type": "disabled"},
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": compile_schema(request.output_kind, "anthropic").schema,
                }
            },
        }

    def estimate(self, request: ModelRequest) -> TokenCount:
        return estimate_body(self.body(request))

    async def count(self, request: ModelRequest) -> TokenCount:
        body = self.body(request)
        body.pop("max_tokens")
        body.pop("service_tier")
        result = await self.client.messages.count_tokens(**body)
        return TokenCount(result.input_tokens, True, "anthropic_native_count_estimate")

    async def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent]:
        raw = ""
        model = response_id = ""
        started = False
        block: tuple[int, str] | None = None
        next_index = 0
        stop_reason = None
        counts: dict[str, Any] = {}
        terminal_usage = False
        try:
            async with asyncio.timeout(60):
                stream = await self.client.messages.create(**self.body(request), stream=True)
                async with stream:
                    async for event in stream:
                        value = event.model_dump()
                        kind = value.get("type")
                        if kind == "message_start":
                            if started:
                                raise ValueError("Duplicate message start")
                            message = value["message"]
                            if message.get("content"):
                                raise ValueError("Unexpected prefilled content")
                            model, response_id = message["model"], message["id"]
                            counts = dict(message.get("usage") or {})
                            started = True
                        elif not started:
                            if kind == "ping":
                                continue
                            raise ValueError("Missing message start")
                        elif kind == "content_block_start":
                            if block is not None or value["index"] != next_index:
                                raise ValueError("Invalid block order")
                            content = value["content_block"]
                            category = content["type"]
                            if category not in ("text", "thinking", "redacted_thinking"):
                                yield ModelFault("unexpected_provider_tool", "ambiguous")
                                return
                            block = (value["index"], category)
                            if category == "text" and content.get("text"):
                                raw += content["text"]
                                if len(raw.encode()) > 200000:
                                    raise ValueError("Output bound")
                                yield TextDelta(content["text"])
                        elif kind == "content_block_delta":
                            if block is None or value["index"] != block[0]:
                                raise ValueError("Invalid delta index")
                            delta = value["delta"]
                            if delta["type"] == "text_delta" and block[1] == "text":
                                raw += delta["text"]
                                if len(raw.encode()) > 200000:
                                    raise ValueError("Output bound")
                                yield TextDelta(delta["text"])
                            elif delta["type"] not in (
                                "thinking_delta",
                                "signature_delta",
                            ) or block[1] not in ("thinking", "redacted_thinking"):
                                raise ValueError("Unexpected content delta")
                            # Thinking/signatures stay private and never enter ModelResult/raw_text.
                        elif kind == "content_block_stop":
                            if block is None or value["index"] != block[0]:
                                raise ValueError("Invalid block stop")
                            block = None
                            next_index += 1
                        elif kind == "message_delta":
                            if block is not None:
                                raise ValueError("Unclosed content block")
                            delta = value["delta"]
                            if delta.get("stop_reason"):
                                stop_reason = delta["stop_reason"]
                            terminal_usage = (value.get("usage") or {}).get(
                                "output_tokens"
                            ) is not None
                            for key, item in (value.get("usage") or {}).items():
                                if item is not None:
                                    if isinstance(item, int) and item < (counts.get(key) or 0):
                                        raise ValueError("Regressing cumulative usage")
                                    counts[key] = item
                        elif kind == "message_stop":
                            if block is not None or stop_reason is None:
                                raise ValueError("Incomplete terminal")
                            if any((counts.get("server_tool_use") or {}).values()):
                                yield ModelFault("unsupported_server_tool_charge", "ambiguous")
                                return
                            if counts.get("cache_creation_input_tokens"):
                                yield ModelFault("unsupported_cache_write_charge", "ambiguous")
                                return
                            usage = None
                            if (
                                terminal_usage
                                and counts.get("input_tokens") is not None
                                and counts.get("output_tokens") is not None
                            ):
                                cached = counts.get("cache_read_input_tokens") or 0
                                details = counts.get("output_tokens_details") or {}
                                usage = Usage(
                                    input_tokens=counts["input_tokens"] + cached,
                                    output_tokens=counts["output_tokens"],
                                    cached_input_tokens=cached,
                                    reasoning_output_tokens=details.get("thinking_tokens", 0),
                                )
                            status: Literal["completed", "refused", "incomplete", "invalid"] = (
                                "completed"
                                if stop_reason == "end_turn"
                                else "refused"
                                if stop_reason == "refusal"
                                else "incomplete"
                                if stop_reason in ("max_tokens", "model_context_window_exceeded")
                                else "invalid"
                            )
                            answer = None
                            if status == "completed":
                                try:
                                    answer = parse_output(request.output_kind, raw)
                                except ValueError, TypeError, RecursionError:
                                    status = "invalid"
                            yield ModelResult(status, answer, usage, model, response_id, raw)
                            return
                        elif kind != "ping":
                            yield ModelFault("provider_protocol_error", "ambiguous")
                            return
            yield ModelFault("provider_missing_terminal", "ambiguous")
        except APIStatusError as exc:
            yield http_fault(exc.status_code, exc.response.headers.get("retry-after"))
        except APIConnectionError, TimeoutError, httpx2.HTTPError:
            yield ModelFault("provider_transport_interrupted", "ambiguous")
        except APIError:
            yield ModelFault("provider_failed", "ambiguous")
        except ValueError, TypeError, KeyError, AttributeError:
            yield ModelFault("provider_protocol_error", "ambiguous")

"""Native Responses transport. One invocation is exactly one SDK attempt."""

import asyncio
import json
import math
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, Literal, cast

import httpx2
from evidencedesk.providers.base import (
    ModelEvent,
    ModelFault,
    ModelRequest,
    ModelResult,
    TextDelta,
    TokenCount,
    Usage,
)
from evidencedesk.providers.schema import output_format, parse_output
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from openai.types.responses import ResponseInputParam, ResponseTextConfigParam


class OpenAIAdapter:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        tokenizer_cache: Path = Path(".cache/models"),
    ):
        self.tokenizer_cache = tokenizer_cache
        # Fixed official origin; environment proxies and redirects cannot reroute private prompts.
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.openai.com/v1",
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
        # Context stays in the untrusted user channel; it never becomes a policy instruction.
        messages.append(
            {
                "role": "user",
                "content": f"<evidence>\n{request.context}\n</evidence>\n<question>\n{request.query}\n</question>",
            }
        )
        return {
            "model": request.model_id,
            "instructions": request.instructions,
            "input": messages,
            "text": {"format": output_format(request.output_kind)},
            "reasoning": {"effort": "none"},
            "max_output_tokens": request.max_output_tokens,
            "store": False,
            "stream": True,
            "truncation": "disabled",
        }

    def estimate(self, request: ModelRequest) -> TokenCount:
        # Conservative full serialized request estimate, explicitly not a provider-exact count.
        from evidencedesk.models.tokenizer import generation_tokenizer

        tokenizer = generation_tokenizer(self.tokenizer_cache)
        serialized = json.dumps(self.body(request), ensure_ascii=False, separators=(",", ":"))
        return TokenCount(
            math.ceil(len(tokenizer.encode(serialized, disallowed_special=())) * 1.2),
            True,
            "o200k_full_json_plus_20_percent",
        )

    async def count(self, request: ModelRequest) -> TokenCount:
        # This also sends content to the provider; orchestration must authorize its data boundary.
        body = self.body(request)
        result = await self.client.responses.input_tokens.count(
            model=request.model_id,
            instructions=request.instructions,
            input=cast(ResponseInputParam, body["input"]),
            text=cast(ResponseTextConfigParam, body["text"]),
            reasoning={"effort": "none"},
            truncation="disabled",
        )
        return TokenCount(result.input_tokens, False, "responses_input_tokens")

    async def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent]:
        body = self.body(request)
        text = ""
        refusal = False
        terminal = False
        try:
            async with asyncio.timeout(60):
                stream = await self.client.responses.create(
                    model=request.model_id,
                    instructions=request.instructions,
                    input=cast(ResponseInputParam, body["input"]),
                    text=cast(ResponseTextConfigParam, body["text"]),
                    reasoning={"effort": "none"},
                    max_output_tokens=request.max_output_tokens,
                    store=False,
                    stream=True,
                    truncation="disabled",
                )
                async with stream:
                    iterator = stream.__aiter__()
                    while True:
                        try:
                            async with asyncio.timeout(10):
                                event = await anext(iterator)
                        except StopAsyncIteration:
                            break
                        if event.type == "response.output_text.delta":
                            text += event.delta
                            if len(text.encode()) > 200_000:
                                yield ModelFault("provider_output_too_large", "ambiguous")
                                return
                            yield TextDelta(event.delta)
                        elif event.type in ("response.refusal.delta", "response.refusal.done"):
                            refusal = True
                        elif event.type in ("response.completed", "response.incomplete"):
                            terminal = True
                            response = event.response
                            usage = None
                            if response.usage:
                                usage = Usage(
                                    input_tokens=response.usage.input_tokens,
                                    output_tokens=response.usage.output_tokens,
                                    cached_input_tokens=response.usage.input_tokens_details.cached_tokens,
                                    reasoning_output_tokens=response.usage.output_tokens_details.reasoning_tokens,
                                )
                            final_parts = []
                            for item in response.output:
                                if item.type == "message":
                                    for part in item.content:
                                        if part.type == "refusal":
                                            refusal = True
                                        elif part.type == "output_text":
                                            final_parts.append(part.text)
                                elif item.type != "reasoning":
                                    yield ModelFault("unexpected_provider_tool", "ambiguous")
                                    return
                            raw = "".join(final_parts)
                            if len(raw.encode()) > 200_000:
                                yield ModelFault("provider_output_too_large", "ambiguous")
                                return
                            if text and raw != text:
                                yield ModelFault("provider_text_mismatch", "ambiguous")
                                return
                            status: Literal["completed", "refused", "incomplete", "invalid"] = (
                                "refused"
                                if refusal
                                else "incomplete"
                                if event.type == "response.incomplete"
                                else "completed"
                            )
                            answer = None
                            if status == "completed":
                                try:
                                    answer = parse_output(request.output_kind, raw)
                                except ValueError, TypeError, RecursionError:
                                    status = "invalid"
                            yield ModelResult(
                                status, answer, usage, response.model, response.id, raw
                            )
                            return  # Only the first authoritative terminal can settle usage.
                        elif event.type in ("response.failed", "error"):
                            yield ModelFault("provider_failed", "ambiguous")
                            return
                        elif event.type == "response.output_item.added" and event.item.type not in (
                            "message",
                            "reasoning",
                        ):
                            yield ModelFault("unexpected_provider_tool", "ambiguous")
                            return
            if not terminal:
                yield ModelFault("provider_missing_terminal", "ambiguous")
        except APIStatusError as exc:
            code = (
                "provider_rate_limited"
                if exc.status_code == 429
                else "provider_context_overflow"
                if exc.status_code == 400 and exc.code == "context_length_exceeded"
                else "provider_configuration"
                if exc.status_code in (400, 401, 403, 404, 422)
                else "provider_unavailable"
            )
            # Gateway errors do not establish that generation was never executed.
            definite = exc.status_code in (400, 401, 403, 404, 422, 429)
            retry_after = None
            try:
                retry_after = max(0.0, float(exc.response.headers.get("retry-after", "0")))
            except ValueError:
                pass
            yield ModelFault(
                code,
                "definite_failure" if definite else "ambiguous",
                exc.status_code == 429,
                retry_after,
            )
        except APIConnectionError, TimeoutError, httpx2.HTTPError:
            yield ModelFault("provider_transport_interrupted", "ambiguous")
        except ValueError, TypeError, AttributeError:
            yield ModelFault("provider_protocol_error", "ambiguous")

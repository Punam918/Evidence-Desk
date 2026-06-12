"""Native Gemini Developer API adapter; one candidate, no tools, bounded private thinking."""

import asyncio
import json
import math
import re
from collections.abc import AsyncGenerator
from contextlib import aclosing
from contextvars import ContextVar
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
from evidencedesk.providers.schema import parse_output
from evidencedesk.providers.secondary_common import estimate_body, http_fault
from evidencedesk.providers.secondary_schema import compile_schema
from google import genai
from google.genai import errors, types


class GoogleAdapter:
    def __init__(self, api_key: str, *, transport: httpx2.AsyncBaseTransport | None = None):
        self.responses: ContextVar[list[httpx2.Response] | None] = ContextVar(
            "google_responses", default=None
        )
        self.http = httpx2.AsyncClient(
            transport=transport,
            event_hooks={"response": [self.track_response]},
            trust_env=False,
            follow_redirects=False,
            timeout=httpx2.Timeout(10, connect=3),
        )
        self.client = genai.Client(
            vertexai=False,
            api_key=api_key,
            http_options=types.HttpOptions(
                base_url="https://generativelanguage.googleapis.com",
                api_version="v1beta",
                timeout=10000,
                httpx_async_client=self.http,
                client_args={"trust_env": False, "follow_redirects": False},
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    async def track_response(self, response: httpx2.Response) -> None:
        owned = self.responses.get()
        if owned is not None:
            owned.append(response)

    async def close(self) -> None:
        await self.client.aio.aclose()
        await self.http.aclose()
        self.client.close()

    @staticmethod
    def body(request: ModelRequest) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", request.model_id):
            raise ValueError("Require a configured Gemini model alias, not a resource URL")
        contents = [
            {"role": "user" if role == "user" else "model", "parts": [{"text": value}]}
            for role, value in request.history
        ]
        contents.append(
            {
                "role": "user",
                "parts": [
                    {
                        "text": f"<evidence>\n{request.context}\n</evidence>\n<question>\n{request.query}\n</question>"
                    }
                ],
            }
        )
        return {
            "model": request.model_id,
            "contents": contents,
            "config": {
                "system_instruction": request.instructions,
                "candidate_count": 1,
                "service_tier": "standard",
                "max_output_tokens": request.max_output_tokens,
                "response_mime_type": "application/json",
                "response_json_schema": compile_schema(request.output_kind, "google").schema,
                "automatic_function_calling": {"disable": True},
                "thinking_config": {"include_thoughts": False, "thinking_level": "MINIMAL"},
            },
        }

    def estimate(self, request: ModelRequest) -> TokenCount:
        return estimate_body(self.body(request))

    async def count(self, request: ModelRequest) -> TokenCount:
        # Developer API SDK count_tokens cannot accept system/schema configuration. Count the full
        # serialized request as text and label the result estimated; never call a contents-only count exact.
        serialized = json.dumps(self.body(request), ensure_ascii=False, separators=(",", ":"))
        result = await self.client.aio.models.count_tokens(
            model=request.model_id, contents=serialized
        )
        if result.total_tokens is None:
            raise ValueError("Provider omitted its token count")
        return TokenCount(
            math.ceil(result.total_tokens * 1.2) + 64,
            True,
            "google_native_serialized_request_estimate",
        )

    async def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent]:
        raw = ""
        usage: Usage | None = None
        reason = None
        blocked = False
        resolved = request.model_id
        response_id = ""
        responses: list[httpx2.Response] = []
        try:
            async with asyncio.timeout(60):
                token = self.responses.set(responses)
                try:
                    iterator = await self.client.aio.models.generate_content_stream(
                        **self.body(request)
                    )
                finally:
                    self.responses.reset(token)
                stream = cast(AsyncGenerator[types.GenerateContentResponse], iterator)
                async with aclosing(stream):
                    while True:
                        # Context ownership ends before yielding to the caller, including when
                        # independent streams are interleaved in the same task.
                        token = self.responses.set(responses)
                        try:
                            response = await anext(stream)
                        except StopAsyncIteration:
                            break
                        finally:
                            self.responses.reset(token)
                        if response.model_version:
                            if resolved != request.model_id and resolved != response.model_version:
                                raise ValueError("Model changed within a response")
                            resolved = response.model_version
                        if response.response_id:
                            if response_id and response_id != response.response_id:
                                raise ValueError("Response identity changed")
                            response_id = response.response_id
                        if response.prompt_feedback and response.prompt_feedback.block_reason:
                            blocked = True
                        metadata = response.usage_metadata
                        if metadata:
                            if metadata.tool_use_prompt_token_count:
                                yield ModelFault("unexpected_provider_tool", "ambiguous")
                                return
                            if (
                                metadata.prompt_token_count is not None
                                and metadata.candidates_token_count is not None
                            ):
                                thoughts = metadata.thoughts_token_count or 0
                                current = Usage(
                                    input_tokens=metadata.prompt_token_count,
                                    output_tokens=metadata.candidates_token_count + thoughts,
                                    cached_input_tokens=metadata.cached_content_token_count or 0,
                                    reasoning_output_tokens=thoughts,
                                )
                                if (
                                    metadata.total_token_count is not None
                                    and metadata.total_token_count
                                    != current.input_tokens + current.output_tokens
                                ):
                                    raise ValueError("Inconsistent inclusive usage")
                                if usage and (
                                    current.input_tokens < usage.input_tokens
                                    or current.output_tokens < usage.output_tokens
                                ):
                                    raise ValueError("Regressing cumulative usage")
                                usage = current
                        candidates = response.candidates or []
                        if len(candidates) > 1:
                            raise ValueError("More than one candidate")
                        if not candidates:
                            continue
                        candidate = candidates[0]
                        if candidate.index not in (None, 0):
                            raise ValueError("Unexpected candidate index")
                        if candidate.grounding_metadata:
                            yield ModelFault("unexpected_provider_tool", "ambiguous")
                            return
                        parts = candidate.content.parts if candidate.content else []
                        for part in parts or []:
                            if any(
                                (
                                    part.function_call,
                                    part.function_response,
                                    part.executable_code,
                                    part.code_execution_result,
                                    part.file_data,
                                    part.inline_data,
                                )
                            ):
                                yield ModelFault("unexpected_provider_tool", "ambiguous")
                                return
                            if part.thought:
                                continue  # Hidden thought text/signatures are never emitted or retained.
                            if part.text:
                                if reason is not None:
                                    raise ValueError("Text after finish reason")
                                raw += part.text
                                if len(raw.encode()) > 200000:
                                    raise ValueError("Output bound")
                                yield TextDelta(part.text)
                        if candidate.finish_reason:
                            if reason is not None and reason != candidate.finish_reason.value:
                                raise ValueError("Conflicting finish reasons")
                            reason = candidate.finish_reason.value
            if blocked:
                status: Literal["completed", "refused", "incomplete", "invalid"] = "refused"
            elif reason == "STOP":
                status = "completed"
            elif reason in (
                "SAFETY",
                "RECITATION",
                "BLOCKLIST",
                "PROHIBITED_CONTENT",
                "SPII",
                "IMAGE_SAFETY",
            ):
                status = "refused"
            elif reason == "MAX_TOKENS":
                status = "incomplete"
            elif reason is not None:
                status = "invalid"
            else:
                yield ModelFault("provider_missing_terminal", "ambiguous")
                return
            answer = None
            if status == "completed":
                try:
                    answer = parse_output(request.output_kind, raw)
                except ValueError, TypeError, RecursionError:
                    status = "invalid"
            yield ModelResult(status, answer, usage, resolved, response_id, raw)
        except errors.APIError as exc:
            headers = getattr(getattr(exc, "response", None), "headers", {})
            yield http_fault(exc.code, headers.get("retry-after"))
        except httpx2.HTTPError, TimeoutError:
            yield ModelFault("provider_transport_interrupted", "ambiguous")
        except ValueError, TypeError, KeyError, AttributeError:
            yield ModelFault("provider_protocol_error", "ambiguous")
        finally:
            # The SDK's outer generator does not close its nested HTTP stream on early
            # cancellation in2.29.0. Public HTTP hooks give this invocation explicit ownership.
            for owned_response in responses:
                await owned_response.aclose()

"""Native local NDJSON adapter, pinned model digest, no hosted fallback path."""

import asyncio
import ipaddress
import json
import re
from collections.abc import AsyncGenerator
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
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
from pydantic import BaseModel, ConfigDict, Field


class TokenCounter(Protocol):
    def count(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> TokenCount: ...


class LocalMessage(BaseModel):
    model_config = ConfigDict(strict=True)
    role: Literal["assistant"] = "assistant"
    content: str = ""
    thinking: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class LocalChunk(BaseModel):
    model_config = ConfigDict(strict=True)
    model: str
    done: bool
    message: LocalMessage = Field(default_factory=LocalMessage)
    done_reason: str | None = None
    prompt_eval_count: int | None = Field(default=None, ge=0)
    prompt_eval_cached_count: int = Field(default=0, ge=0)
    eval_count: int | None = Field(default=None, ge=0)


class OllamaAdapter:
    def __init__(
        self,
        endpoint: str,
        model_id: str,
        digest: str,
        tokenizer: TokenCounter,
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        first_event_timeout: float = 45,
        idle_timeout: float = 10,
        total_timeout: float = 85,
    ):
        parsed = urlsplit(endpoint)
        local_http = parsed.hostname in ("localhost", "127.0.0.1", "::1", "ollama")
        if (
            endpoint != endpoint.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in endpoint)
            or "\\" in endpoint
            or "@" in parsed.netloc
            or "%" in (parsed.hostname or "")
            or parsed.port == 0
            or parsed.scheme not in ("http", "https")
            or (parsed.scheme == "http" and not local_http)
            or not parsed.hostname
            or parsed.username
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError(
                "Require a fixed operator HTTPS endpoint or loopback/Compose Ollama origin"
            )
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if address and (address.is_link_local or address.is_unspecified or address.is_multicast):
            raise ValueError("Unsafe local endpoint address")
        if address is None and re.fullmatch(r"(?:[0-9.]+|0[xX][0-9a-fA-F]+)", parsed.hostname):
            raise ValueError("Noncanonical numeric endpoint address")
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or model_id.endswith(":latest"):
            raise ValueError("Require an exact model name and SHA256 digest")
        if not 0 < first_event_timeout <= total_timeout or not 0 < idle_timeout <= total_timeout:
            raise ValueError("Local stream timeouts must fit within the total request deadline")
        self.model_id, self.digest, self.tokenizer = model_id, digest, tokenizer
        self.first_event_timeout = first_event_timeout
        self.idle_timeout = idle_timeout
        self.total_timeout = total_timeout
        self.client = httpx.AsyncClient(
            base_url=endpoint.rstrip("/"),
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            # The explicit first-event/idle scopes below enforce useful stream
            # semantics. httpx must not preempt a cold CPU model before then.
            timeout=httpx.Timeout(total_timeout, connect=3),
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )

    async def close(self) -> None:
        await self.client.aclose()

    def body(self, request: ModelRequest) -> dict[str, Any]:
        if request.model_id != self.model_id or request.context_cap > 8192:
            raise ValueError("Request does not match the pinned local model/context")
        messages = [{"role": "system", "content": request.instructions}]
        messages.extend({"role": role, "content": content} for role, content in request.history)
        messages.append(
            {
                "role": "user",
                "content": f"<evidence>\n{request.context}\n</evidence>\n<question>\n{request.query}\n</question>",
            }
        )
        return {
            "model": self.model_id,
            "messages": messages,
            "format": output_format(request.output_kind)["schema"],
            "stream": True,
            "think": False,
            "options": {
                "num_ctx": request.context_cap,
                "num_predict": request.max_output_tokens,
                "temperature": 0,
            },
            "keep_alive": "5m",
        }

    def estimate(self, request: ModelRequest) -> TokenCount:
        body = self.body(request)
        return self.tokenizer.count(body["messages"], body["format"])

    async def verify_digest(self) -> bool:
        response = await self.client.get("/api/tags")
        response.raise_for_status()
        models = response.json()["models"]
        return any(
            model.get("name") == self.model_id and model.get("digest") == self.digest
            for model in models
        )

    async def stream(self, request: ModelRequest) -> AsyncGenerator[ModelEvent]:
        issued = False
        raw = ""
        try:
            body = self.body(request)
            if not await self.verify_digest():
                yield ModelFault("local_model_digest_mismatch", "definite_failure")
                return
            async with asyncio.timeout(self.total_timeout):
                issued = True
                async with self.client.stream("POST", "/api/chat", json=body) as response:
                    if response.status_code != 200:
                        definite = response.status_code in (400, 401, 403, 404, 422, 429)
                        yield ModelFault(
                            "local_model_unavailable",
                            "definite_failure" if definite else "ambiguous",
                            response.status_code == 429,
                        )
                        return
                    buffer = b""
                    iterator = response.aiter_bytes().__aiter__()
                    first_event = True
                    while True:
                        try:
                            async with asyncio.timeout(
                                self.first_event_timeout if first_event else self.idle_timeout
                            ):
                                data = await anext(iterator)
                        except StopAsyncIteration:
                            break
                        first_event = False
                        buffer += data
                        if len(buffer) > 1024 * 1024:
                            yield ModelFault("provider_output_too_large", "ambiguous")
                            return
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            if not line.strip():
                                continue
                            value = json.loads(line.decode("utf-8"))
                            if "error" in value:
                                yield ModelFault("local_generation_failed", "ambiguous")
                                return
                            chunk = LocalChunk.model_validate(value)
                            if (
                                chunk.model != self.model_id
                                or chunk.message.thinking
                                or chunk.message.tool_calls
                            ):
                                yield ModelFault("local_conformance_failed", "ambiguous")
                                return
                            if chunk.message.content:
                                raw += chunk.message.content
                                if len(raw.encode()) > 200_000:
                                    yield ModelFault("provider_output_too_large", "ambiguous")
                                    return
                                yield TextDelta(chunk.message.content)
                            if chunk.done:
                                usage = None
                                if (
                                    chunk.prompt_eval_count is not None
                                    and chunk.eval_count is not None
                                ):
                                    usage = Usage(
                                        input_tokens=chunk.prompt_eval_count,
                                        output_tokens=chunk.eval_count,
                                        cached_input_tokens=chunk.prompt_eval_cached_count,
                                    )
                                status: Literal["completed", "incomplete", "invalid"] = (
                                    "completed" if chunk.done_reason == "stop" else "incomplete"
                                )
                                answer = None
                                if status == "completed":
                                    try:
                                        answer = parse_output(request.output_kind, raw)
                                    except ValueError, TypeError, RecursionError:
                                        status = "invalid"
                                yield ModelResult(
                                    status,
                                    answer,
                                    usage,
                                    f"{self.model_id}@sha256:{self.digest}",
                                    "",
                                    raw,
                                )
                                return
            yield ModelFault("provider_missing_terminal", "ambiguous")
        except httpx.HTTPError, TimeoutError:
            yield ModelFault(
                "provider_transport_interrupted", "ambiguous" if issued else "definite_failure"
            )
        except ValueError, TypeError, KeyError, AttributeError:
            yield ModelFault(
                "provider_protocol_error", "ambiguous" if issued else "definite_failure"
            )

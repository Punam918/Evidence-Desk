"""Synthetic native capability checks. These never contain workspace or user content."""

import asyncio
from contextlib import aclosing
from dataclasses import dataclass

from evidencedesk.models.registry import ModelConfig
from evidencedesk.providers.base import ModelAdapter, ModelRequest, ModelResult, TextDelta, Usage


@dataclass(frozen=True)
class ProbeResult:
    schema: bool = False
    streaming: bool = False
    cancellation: bool = False
    usage: bool = False


def probe_request(model: ModelConfig) -> ModelRequest:
    return ModelRequest(
        model.key,
        model.model_id,
        'Return the required JSON: status "answered", one sentence citing E1, no limitations. '
        "Answer only from the supplied evidence; include no reasoning.",
        "How long are records retained?",
        "E1: Records are retained for 30 days.",
        context_cap=model.application_context_cap,
        max_output_tokens=160,
    )


def probe_reservation(adapter: ModelAdapter, model: ModelConfig) -> int:
    request = probe_request(model)
    # The cancellation request can finish remotely, so reserve its full output too.
    return 2 * model.prices.charge(
        Usage(
            input_tokens=adapter.estimate(request).tokens, output_tokens=request.max_output_tokens
        )
    )


async def probe(adapter: ModelAdapter, model: ModelConfig) -> ProbeResult:
    request = probe_request(model)
    streaming = schema = usage = cancellation = False
    async with asyncio.timeout(70):
        async with aclosing(adapter.stream(request)) as stream:
            async for event in stream:
                if isinstance(event, TextDelta) and event.text:
                    streaming = True
                if isinstance(event, ModelResult):
                    schema = bool(
                        event.status == "completed"
                        and event.answer
                        and event.answer.status == "answered"
                        and event.answer.sentences
                        and all(s.citation_ids == ["E1"] for s in event.answer.sentences)
                    )
                    usage = event.usage is not None and event.usage.input_tokens > 0
                    break
        if not (streaming and schema and usage):
            return ProbeResult(schema, streaming, False, usage)
        # Observe a first delta then explicitly close. This proves client-side cancellation;
        # it cannot establish that an upstream service stopped compute or charging.
        async with aclosing(adapter.stream(request)) as stream:
            async for event in stream:
                if isinstance(event, TextDelta) and event.text:
                    async with asyncio.timeout(3):
                        await stream.aclose()
                    cancellation = True
                    break
                if isinstance(event, ModelResult):
                    break
    return ProbeResult(schema, streaming, cancellation, usage)

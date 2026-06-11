"""Lossy OTLP/protobuf transport with no proxy, redirects, retries or response-body logging."""

import time
from collections.abc import Sequence

import httpx
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from evidencedesk.observability.config import TelemetrySettings


class PrivateOTLPExporter(SpanExporter):
    def __init__(self, endpoint: str, *, transport: httpx.BaseTransport | None = None):
        self.endpoint = TelemetrySettings.private_collector(endpoint)
        self.client = httpx.Client(
            timeout=1,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )
        self.closed = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self.closed or self.endpoint is None:
            return SpanExportResult.FAILURE
        try:
            payload = encode_spans(spans).SerializeToString()
            if len(payload) > 262144:
                return SpanExportResult.FAILURE
            deadline = time.monotonic() + 1
            with self.client.stream(
                "POST",
                self.endpoint,
                content=payload,
                headers={"Content-Type": "application/x-protobuf"},
            ) as response:
                if response.status_code != 200:
                    return SpanExportResult.FAILURE
                body = bytearray()
                for part in response.iter_bytes(chunk_size=4096):
                    body.extend(part)
                    if len(body) > 8192 or time.monotonic() > deadline:
                        return SpanExportResult.FAILURE
                decoded = ExportTraceServiceResponse.FromString(bytes(body))
                return (
                    SpanExportResult.FAILURE
                    if decoded.partial_success.rejected_spans
                    else SpanExportResult.SUCCESS
                )
        except Exception:
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self.closed = True
        self.client.close()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

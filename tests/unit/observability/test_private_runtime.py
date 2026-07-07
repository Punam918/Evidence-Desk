import asyncio
import io
import time
from uuid import uuid4

import httpx
import pytest
from evidencedesk.observability.config import TelemetrySettings, reject_automatic_content_tracing
from evidencedesk.observability.exporter import PrivateOTLPExporter
from evidencedesk.observability.listener import MetricsListener
from evidencedesk.observability.stages import current, stage
from evidencedesk.observability.telemetry import Telemetry
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com/v1/traces",
        "http://127.0.0.1:4317",
        "http://secret@collector:4318/v1/traces",
        "http://collector:4318/v1/traces?key=secret",
        "http://169.254.169.254/v1/traces",
    ],
)
def test_collector_destinations_do_not_accept_public_or_credential_urls(endpoint):
    with pytest.raises(ValueError):
        TelemetrySettings(otlp_endpoint=endpoint)


def test_runtime_configuration_requires_mounted_token_and_disables_content_tracing(
    tmp_path, monkeypatch
):
    with pytest.raises(ValueError):
        TelemetrySettings(metrics_port=9108)
    token = tmp_path / "token"
    token.write_text("short")
    settings = TelemetrySettings(metrics_port=0, metrics_token_file=token)
    with pytest.raises(ValueError):
        settings.token()
    token.write_text("a" * 64)
    assert settings.token() == b"a" * 64
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    with pytest.raises(ValueError, match="Automatic content"):
        reject_automatic_content_tracing()


async def test_private_scrape_auth_routes_header_limits_and_shutdown():
    token = b"test-token-" + b"a" * 40
    listener = MetricsListener(lambda: b"ed_test 1\n", token)
    await listener.start("127.0.0.1", 0)
    port = listener.server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False
        ) as client:
            assert (await client.get("/metrics")).status_code == 401
            assert (
                await client.get("/metrics", headers={"Authorization": "Bearer wrong"})
            ).status_code == 401
            headers = {"Authorization": "Bearer " + token.decode()}
            good = await client.get("/metrics", headers=headers)
            assert good.status_code == 200 and good.text == "ed_test 1\n"
            assert good.headers["cache-control"] == "no-store"
            assert (await client.get("/metrics?SECRET-CANARY", headers=headers)).status_code == 404
            assert (await client.post("/metrics", headers=headers)).status_code == 404
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /metrics HTTP/1.1\r\nX-Large: " + b"x" * 9000 + b"\r\n\r\n")
        await writer.drain()
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /metrics HTTP/1.1\r\n")
        await writer.drain()
        await asyncio.sleep(0.01)
        await listener.close()
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()
    finally:
        await listener.close()


@pytest.mark.parametrize(
    "outcome", ["ok", "partial", "redirect", "oversized", "invalid", "timeout"]
)
def test_otlp_native_protobuf_bounded_transport_without_secret_logging(
    outcome, caplog, monkeypatch
):
    memory = InMemorySpanExporter()
    telemetry = Telemetry(
        frozenset({"/health/live"}), exporter=memory, log_stream=io.StringIO(), sample_rate=1
    )
    try:
        telemetry.record_http("/health/live", "GET", 200, time.time_ns(), 0.01, uuid4())
        telemetry.provider.force_flush()
        spans = memory.get_finished_spans()
        calls = []

        def transport(request):
            calls.append(request)
            assert "authorization" not in request.headers
            assert "ENV-SECRET-CANARY" not in request.content.decode(errors="ignore")
            decoded = ExportTraceServiceRequest.FromString(request.content)
            assert decoded.resource_spans[0].scope_spans[0].spans[0].name == "http.request"
            if outcome == "timeout":
                raise httpx.ReadTimeout("UPSTREAM-SECRET-CANARY")
            if outcome == "redirect":
                return httpx.Response(
                    307,
                    headers={"Location": "https://untrusted.test/"},
                    content=b"UPSTREAM-SECRET-CANARY",
                )
            if outcome == "oversized":
                return httpx.Response(200, content=b"x" * 9000)
            if outcome == "invalid":
                return httpx.Response(200, content=b"UPSTREAM-SECRET-CANARY")
            result = ExportTraceServiceResponse()
            if outcome == "partial":
                result.partial_success.rejected_spans = 1
                result.partial_success.error_message = "UPSTREAM-SECRET-CANARY"
            return httpx.Response(200, content=result.SerializeToString())

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=ENV-SECRET-CANARY")
        exporter = PrivateOTLPExporter(
            "http://collector:4318/v1/traces", transport=httpx.MockTransport(transport)
        )
        try:
            result = exporter.export(spans)
            assert result == (
                SpanExportResult.SUCCESS if outcome == "ok" else SpanExportResult.FAILURE
            )
            assert len(calls) == 1
            assert "SECRET-CANARY" not in caplog.text
        finally:
            exporter.shutdown()
    finally:
        telemetry.close()


def test_stage_traces_never_serialize_checkpoint_state_or_exception():
    memory, stream = InMemorySpanExporter(), io.StringIO()
    telemetry = Telemetry(
        frozenset(), exporter=memory, log_stream=stream, sample_rate=1, service="chat-worker"
    )
    token = current.set(telemetry)
    try:
        with pytest.raises(RuntimeError):
            with stage("checkpoint.write"):
                raise RuntimeError("CHECKPOINT-SECRET-CANARY")
        with stage("arbitrary-SECRET-CANARY"):
            pass
        telemetry.provider.force_flush()
        spans = memory.get_finished_spans()
        assert [s.name for s in spans] == ["checkpoint.write"]
        assert 'event="checkpoint.failure"' in telemetry.scrape().decode()
        assert "SECRET-CANARY" not in str(spans[0].attributes) + telemetry.scrape().decode()
    finally:
        current.reset(token)
        telemetry.close()
    assert "SECRET-CANARY" not in stream.getvalue()

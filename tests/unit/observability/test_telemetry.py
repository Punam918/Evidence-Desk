import io
import logging
import queue
import time
from uuid import uuid4

from evidencedesk.observability.telemetry import (
    BoundedQueueHandler,
    HTTPMetricsMiddleware,
    Telemetry,
)
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client.parser import text_string_to_metric_families


def test_allowlist_cardinality_canary_and_tail_sampling():
    stream = io.StringIO()
    exporter = InMemorySpanExporter()
    telemetry = Telemetry(
        frozenset({"/v1/workspaces/{workspace_id}/health"}),
        exporter=exporter,
        log_stream=stream,
        sample_rate=0,
    )
    try:
        for _ in range(20):
            telemetry.record_http(
                "/private/SECRET-CANARY/" + str(uuid4()),
                "SECRET-CANARY",
                404,
                time.time_ns(),
                0.01,
                "SECRET-CANARY",
            )
        telemetry.record_http(
            "/v1/workspaces/{workspace_id}/health", "GET", 500, time.time_ns(), 0.02, uuid4()
        )
        assert telemetry.provider.force_flush(timeout_millis=2000)
        spans = exporter.get_finished_spans()
        assert len(spans) == 1 and spans[0].status.is_ok is False
        assert not spans[0].events and not spans[0].links
        assert "SECRET-CANARY" not in str(spans[0].attributes)
        metrics = telemetry.scrape().decode()
        assert "SECRET-CANARY" not in metrics
        labels = {
            tuple(sorted(s.labels.items()))
            for family in text_string_to_metric_families(metrics)
            for s in family.samples
            if s.name == "ed_http_requests_total"
        }
        assert len(labels) == 2
    finally:
        telemetry.close()
    assert "SECRET-CANARY" not in stream.getvalue()
    assert '"status_class":"5xx"' in stream.getvalue()


class FailingExporter(SpanExporter):
    def export(self, spans):
        raise RuntimeError("EXPORT-SECRET-CANARY")

    def shutdown(self):
        raise RuntimeError("SHUTDOWN-SECRET-CANARY")


def test_export_failure_and_queue_saturation_are_lossy_not_request_failures(caplog):
    stream = io.StringIO()
    telemetry = Telemetry(
        frozenset({"/health/live"}), exporter=FailingExporter(), log_stream=stream, sample_rate=1
    )
    telemetry.record_http("/health/live", "GET", 200, time.time_ns(), 0.01, uuid4())
    telemetry.provider.force_flush(timeout_millis=2000)
    telemetry.close()
    assert "SECRET-CANARY" not in caplog.text + stream.getvalue()
    events = queue.Queue(maxsize=1)
    handler = BoundedQueueHandler(events)
    record = logging.LogRecord("safe", logging.INFO, "", 0, "bounded", (), None)
    handler.emit(record)
    handler.emit(record)
    assert events.qsize() == 1


async def test_stream_duration_and_disconnect_are_recorded_once():
    telemetry = Telemetry(frozenset(), log_stream=io.StringIO(), sample_rate=0)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"PRIVATE-CANARY", "more_body": True})
        raise ConnectionError("PRIVATE-CANARY")

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    try:
        try:
            await HTTPMetricsMiddleware(app, telemetry)(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/PRIVATE-CANARY",
                    "headers": [(b"authorization", b"PRIVATE-CANARY")],
                },
                receive,
                send,
            )
        except ConnectionError:
            pass
        data = telemetry.scrape().decode()
        assert 'status_class="interrupted"} 1.0' in data
        assert "PRIVATE-CANARY" not in data
    finally:
        telemetry.close()

"""Allowlisted HTTP signals. Export failure and saturation cannot change request outcomes."""

import json
import logging
import math
import queue
import random
import time
from collections.abc import Sequence
from logging.handlers import QueueHandler, QueueListener
from typing import TextIO
from uuid import UUID

from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import StatusCode
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from evidencedesk.observability.stages import STAGES, current


class SafeExporter(SpanExporter):
    def __init__(self, exporter: SpanExporter):
        self.exporter = exporter

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            return self.exporter.export(spans)
        except Exception:
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self.exporter.shutdown()
        except Exception:
            pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return self.exporter.force_flush(timeout_millis)
        except Exception:
            return False


class BoundedQueueHandler(QueueHandler):
    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            pass  # Never invoke logging.handleError with request-related diagnostics.


class Telemetry:
    def __init__(
        self,
        routes: frozenset[str],
        *,
        exporter: SpanExporter | None = None,
        log_stream: TextIO | None = None,
        sample_rate: float = 0.1,
        service: str = "api",
    ):
        if not 0 <= sample_rate <= 1:
            raise ValueError("Invalid sampling probability")
        if service not in {
            "api",
            "chat-worker",
            "brief-worker",
            "ingest-worker",
            "index-worker",
            "index-reconcile",
            "model-probe",
            "metrics-worker",
            "checkpoint-prune",
            "replay-prune",
            "conversation-purge-worker",
            "source-redact",
            "conversation-expire",
            "source-objects-purge",
        }:
            raise ValueError("Unregistered telemetry service")
        self.service = service
        self.routes = routes
        self.sample_rate = sample_rate
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "ed_http_requests_total",
            "Completed HTTP responses or interrupted requests",
            ["route_template", "method", "status_class"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "ed_http_duration_seconds",
            "HTTP duration including response streaming",
            ["route_template"],
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.3, 0.5, 1, 2, 5, 10, 30, 60),
        )
        self.stages = Histogram(
            "ed_stage_duration_seconds",
            "Stage latency including failures",
            ["stage", "outcome"],
            registry=self.registry,
            buckets=(0.005, 0.01, 0.05, 0.1, 0.3, 0.5, 1, 2, 5, 10, 30, 60),
        )
        self.operations = Counter(
            "ed_operational_events_total",
            "Bounded process operational events",
            ["event"],
            registry=self.registry,
        )
        self.candidates = Histogram(
            "ed_retrieval_candidates",
            "Candidate counts after PostgreSQL scope and payload validation",
            ["branch"],
            registry=self.registry,
            buckets=(0, 1, 5, 10, 20, 40, 80),
        )
        self.provider = TracerProvider(
            resource=Resource({"service.name": service}),
            shutdown_on_exit=False,
            sampler=ALWAYS_ON,
            span_limits=SpanLimits(max_attributes=8, max_events=0, max_links=0),
        )
        if exporter:
            self.provider.add_span_processor(
                BatchSpanProcessor(
                    SafeExporter(exporter),
                    max_queue_size=512,
                    max_export_batch_size=128,
                    schedule_delay_millis=500,
                    export_timeout_millis=1000,
                )
            )
        self.tracer = self.provider.get_tracer("evidencedesk.http", "1")
        self.logger = logging.Logger("evidencedesk.safe_http", level=logging.INFO)
        self.logger.propagate = False
        events: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=1024)
        self.events = events
        self.logger.addHandler(BoundedQueueHandler(events))
        sink = logging.StreamHandler(log_stream)
        sink.setFormatter(logging.Formatter("%(message)s"))
        self.listener = QueueListener(events, sink)
        self.listener.start()

    def record_http(
        self,
        route: str,
        method: str,
        status: int,
        started_ns: int,
        duration: float,
        request_id: object,
    ) -> None:
        # Resolve only code-owned templates; no URL/path/query/header/body enters any sink.
        route = route if route in self.routes else "unmatched"
        method = (
            method
            if method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
            else "OTHER"
        )
        status_class = str(status // 100) + "xx" if 100 <= status < 600 else "interrupted"
        if not math.isfinite(duration) or duration < 0:
            return
        try:
            self.requests.labels(route, method, status_class).inc()
            self.duration.labels(route).observe(duration)
            attributes: dict[str, str | int] = {
                "http.route": route,
                "http.request.method": method,
                "http.response.status_code": status if 100 <= status < 600 else 0,
            }
            error = status >= 500 or status == 0
            # Tail decision for these standalone HTTP spans keeps all errors and 10% of normal requests.
            if error or random.random() < self.sample_rate:
                span = self.tracer.start_span(
                    "http.request", context=Context(), attributes=attributes, start_time=started_ns
                )
                if error:
                    span.set_status(StatusCode.ERROR)
                span.end(end_time=started_ns + int(duration * 1_000_000_000))
            record = {
                "timestamp_unix_ns": started_ns,
                "level": "ERROR" if error else "INFO",
                "service": self.service,
                "event": "http.completed",
                "route_template": route,
                "method": method,
                "status_class": status_class,
                "duration_ms": round(duration * 1000, 3),
            }
            if isinstance(request_id, UUID):
                record["request_id"] = str(request_id)
            self.logger.info(json.dumps(record, separators=(",", ":")))
        except Exception:
            # Usage/audit commits are separate authoritative operations and never pass through here.
            pass

    def record_stage(self, name: str, outcome: str, started_ns: int, duration: float) -> None:
        if (
            name not in STAGES
            or outcome not in {"ok", "error", "cancelled"}
            or not math.isfinite(duration)
            or duration < 0
        ):
            return
        try:
            self.stages.labels(name, outcome).observe(duration)
            if outcome != "ok" or random.random() < self.sample_rate:
                span = self.tracer.start_span(
                    name,
                    context=Context(),
                    attributes={"ed.stage": name, "ed.outcome": outcome},
                    start_time=started_ns,
                )
                if outcome != "ok":
                    span.set_status(StatusCode.ERROR)
                span.end(end_time=started_ns + int(duration * 1_000_000_000))
            self.logger.info(
                json.dumps(
                    {
                        "timestamp_unix_ns": started_ns,
                        "event": "stage.completed",
                        "service": self.service,
                        "stage": name,
                        "outcome": outcome,
                        "duration_ms": round(duration * 1000, 3),
                    },
                    separators=(",", ":"),
                )
            )
        except Exception:
            pass

    def scrape(self) -> bytes:
        return generate_latest(self.registry)

    def close(self) -> None:
        # Run outside the API event loop. QueueListener.stop needs a free slot for its sentinel.
        while self.events.full():
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        self.listener.stop()
        self.provider.shutdown()


class HTTPMetricsMiddleware:
    def __init__(self, app: ASGIApp, telemetry: Telemetry):
        self.app, self.telemetry = app, telemetry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started_ns, started = time.time_ns(), time.perf_counter()
        status, completed = 0, False

        async def observe(message: Message) -> None:
            nonlocal status, completed
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True

        token = current.set(self.telemetry)
        try:
            await self.app(scope, receive, observe)
        finally:
            current.reset(token)
            template = getattr(scope.get("route"), "path", "unmatched")
            self.telemetry.record_http(
                template,
                scope.get("method", "OTHER"),
                status if completed else 0,
                started_ns,
                time.perf_counter() - started,
                scope.get("state", {}).get("request_id"),
            )

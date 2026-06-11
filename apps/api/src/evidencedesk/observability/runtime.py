import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from evidencedesk.observability.config import TelemetrySettings, reject_automatic_content_tracing
from evidencedesk.observability.exporter import PrivateOTLPExporter
from evidencedesk.observability.listener import MetricsListener
from evidencedesk.observability.stages import current
from evidencedesk.observability.telemetry import Telemetry


def configured(
    service: str, routes: frozenset[str] = frozenset(), settings: TelemetrySettings | None = None
) -> tuple[Telemetry, MetricsListener | None, TelemetrySettings]:
    reject_automatic_content_tracing()
    settings = settings or TelemetrySettings()
    token = settings.token() if settings.metrics_port is not None else None
    telemetry = Telemetry(
        routes,
        service=service,
        exporter=PrivateOTLPExporter(settings.otlp_endpoint) if settings.otlp_endpoint else None,
    )
    listener = MetricsListener(telemetry.scrape, token) if token is not None else None
    return telemetry, listener, settings


@asynccontextmanager
async def runtime(service: str) -> AsyncIterator[Telemetry]:
    telemetry, listener, settings = configured(service)
    token = current.set(telemetry)
    try:
        if listener:
            assert settings.metrics_port is not None
            await listener.start(settings.metrics_host, settings.metrics_port)
        yield telemetry
    finally:
        if listener:
            await listener.close()
        current.reset(token)
        await asyncio.to_thread(telemetry.close)

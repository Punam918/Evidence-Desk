"""Cached fixed-shape DB aggregates; scraping never issues a query or changes accounting."""

import asyncio
import math
import time
from collections.abc import Iterable
from dataclasses import dataclass

from prometheus_client.core import GaugeMetricFamily
from sqlalchemy.ext.asyncio import AsyncEngine

from evidencedesk.db.metrics_repository import snapshot

MODELS = frozenset(
    {
        "paid_standard",
        "local_standard",
        "paid_quality",
        "paid_reference",
        "anthropic_candidate",
        "google_candidate",
        "other",
    }
)
DIMENSIONS = {
    "model_key": MODELS,
    "estimated": frozenset({"true", "false"}),
    "direction": frozenset({"input", "output"}),
    "kind": frozenset({"ingest", "chat", "purge", "summary", "maintenance", "workflow"}),
    "state": frozenset({"queued", "running"}),
}
METRICS = {
    "ed_jobs_queue_age_seconds": ("kind",),
    "ed_jobs_dead": ("kind",),
    "ed_active_runs": ("state",),
    "ed_index_outbox_age_seconds": (),
    "ed_index_outbox_dead": (),
    "ed_index_manifest_unavailable": (),
    "ed_source_purge_age_seconds": (),
    "ed_graph_expired_active": (),
    "ed_approval_overdue": (),
    "ed_ambiguous_usage_unsettled": (),
    "ed_model_cost_micro_usd": ("model_key", "estimated"),
    "ed_model_tokens": ("model_key", "direction"),
    "ed_provider_breaker_state": ("model_key",),
    "ed_provider_probe_age_seconds": ("model_key",),
}


@dataclass(frozen=True)
class Sample:
    name: str
    labels: tuple[str, ...]
    value: float


class LedgerMetrics:
    def __init__(self) -> None:
        self.values: tuple[Sample, ...] = ()
        self.last_success = 0.0
        self.healthy = False

    async def refresh(self, db: AsyncEngine) -> bool:
        try:
            async with asyncio.timeout(2.5):
                rows = await snapshot(db)
            if len(rows) > 256:
                raise ValueError("Unexpected metric cardinality")
            result = []
            identities = set()
            for row in rows:
                name, labels, value = row["name"], row["labels"], row["value"]
                if name not in METRICS or set(labels) != set(METRICS[name]):
                    raise ValueError("Unknown metric")
                if (
                    any(v not in DIMENSIONS[k] for k, v in labels.items())
                    or type(value) not in (float, int)
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError("Invalid metric value")
                identity = (name, tuple(labels[k] for k in METRICS[name]))
                if identity in identities:
                    raise ValueError("Duplicate metric")
                identities.add(identity)
                result.append(Sample(*identity, float(value)))
            self.values = tuple(result)
            self.last_success, self.healthy = time.time(), True
            return True
        except Exception:
            self.values, self.healthy = (), False
            return False

    def collect(self) -> Iterable[GaugeMetricFamily]:
        healthy = self.healthy and time.time() - self.last_success <= 45
        yield GaugeMetricFamily(
            "ed_monitor_snapshot_success",
            "Fresh aggregate snapshot available within 45 seconds",
            value=float(healthy),
        )
        yield GaugeMetricFamily(
            "ed_monitor_last_success_timestamp_seconds",
            "Last successful aggregate snapshot time",
            value=self.last_success,
        )
        if not healthy:
            return
        for name, labels in METRICS.items():
            family = GaugeMetricFamily(
                name,
                "Current operational state or retained ledger total; may decrease on retention",
                labels=list(labels),
            )
            for sample in self.values:
                if sample.name == name:
                    family.add_metric(list(sample.labels), sample.value)
            yield family

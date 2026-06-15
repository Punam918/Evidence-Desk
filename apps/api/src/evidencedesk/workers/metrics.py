import asyncio

from evidencedesk.db.session import engine
from evidencedesk.observability.ledger_metrics import LedgerMetrics
from evidencedesk.observability.stages import current
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MonitorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ED_", extra="ignore")
    monitor_database_url: str = Field(repr=False)


async def monitor(once: bool, settings: MonitorSettings | None = None) -> int:
    settings = settings or MonitorSettings()  # type: ignore[call-arg]
    telemetry = current.get()
    if telemetry is None:
        raise ValueError("Monitor requires its private telemetry runtime")
    db = engine(settings.monitor_database_url, pool_size=1)
    collector = LedgerMetrics()
    telemetry.registry.register(collector)
    try:
        while True:
            healthy = await collector.refresh(db)
            if once:
                return 0 if healthy else 1
            await asyncio.sleep(15)
    finally:
        await db.dispose()

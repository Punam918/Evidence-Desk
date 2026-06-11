"""Dispatcher-only shared breaker and fenced probe ownership; contains no user content."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from evidencedesk.errors import Problem
from evidencedesk.identity.crypto import canonical_json
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class ProbeLease:
    model_key: str
    token: UUID
    config_sha256: str
    endpoint_sha256: str


@dataclass(frozen=True)
class Capabilities:
    schema: bool
    streaming: bool
    cancellation: bool
    usage: bool

    @property
    def passed(self) -> bool:
        return self.schema and self.streaming and self.cancellation and self.usage


class ProviderHealth:
    def __init__(self, dispatch: AsyncEngine):
        self.db = dispatch

    async def claim_probe(
        self, key: str, config_sha256: str, endpoint_sha256: str, *, reserved_micro_usd: int = 0
    ) -> ProbeLease | None:
        if not 0 <= reserved_micro_usd <= 10_000:
            raise ValueError("Invalid operator probe reservation")
        async with self.db.begin() as conn:
            # Serialize fleet-wide probe admission, including different model keys.
            await conn.execute(text("SELECT pg_advisory_xact_lock(76192021)"))
            await conn.execute(
                text("""INSERT INTO app.provider_health(model_key,state,window_start)
                VALUES(:key,'open',clock_timestamp()) ON CONFLICT DO NOTHING"""),
                {"key": key},
            )
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT *,clock_timestamp() AS now FROM app.provider_health WHERE model_key=:key FOR UPDATE"
                        ),
                        {"key": key},
                    )
                )
                .mappings()
                .one()
            )
            now = row["now"]
            if row["state"] == "disabled" or (row["probe_until"] and row["probe_until"] > now):
                return None
            report = row["capability_report"]
            same = (
                report.get("config_sha256") == config_sha256
                and report.get("endpoint_sha256") == endpoint_sha256
            )
            if same and (
                (
                    row["state"] == "closed"
                    and row["last_probe_at"]
                    # Refresh before the five-minute serving freshness limit. The
                    # local probe loop polls every30s and still holds the global
                    # generation slot while running its bounded native probe.
                    and row["last_probe_at"] > now - timedelta(minutes=4)
                )
                or (row["state"] == "open" and row["open_until"] and row["open_until"] > now)
            ):
                return None
            day = (
                await conn.execute(text("SELECT (clock_timestamp() AT TIME ZONE 'UTC')::date"))
            ).scalar_one()
            total = (
                await conn.execute(
                    text(
                        "SELECT COALESCE(sum(probe_reserved_micro_usd),0) FROM app.provider_health WHERE probe_budget_day=:day"
                    ),
                    {"day": day},
                )
            ).scalar_one()
            if total + reserved_micro_usd > 1_000_000:
                raise Problem(
                    429, "probe_budget_exceeded", "The operator probe daily cap is exhausted."
                )
            await conn.execute(
                text("""UPDATE app.provider_health SET probe_budget_day=:day,
                probe_reserved_micro_usd=CASE WHEN probe_budget_day=:day THEN probe_reserved_micro_usd ELSE 0 END+:amount
                WHERE model_key=:key"""),
                {"key": key, "day": day, "amount": reserved_micro_usd},
            )
            lease = ProbeLease(key, uuid4(), config_sha256, endpoint_sha256)
            await conn.execute(
                text("""UPDATE app.provider_health SET state='half_open',probe_token=:token,
                probe_until=clock_timestamp()+interval '90 seconds' WHERE model_key=:key"""),
                {"key": key, "token": lease.token},
            )
            return lease

    async def complete_probe(self, lease: ProbeLease, capabilities: Capabilities) -> bool:
        report = {
            "passed": capabilities.passed,
            "config_sha256": lease.config_sha256,
            "endpoint_sha256": lease.endpoint_sha256,
            "capabilities": {
                "schema": capabilities.schema,
                "streaming": capabilities.streaming,
                "cancellation": capabilities.cancellation,
                "usage": capabilities.usage,
            },
        }
        async with self.db.begin() as conn:
            result = await conn.execute(
                text("""UPDATE app.provider_health SET state=:state,
                last_probe_at=clock_timestamp(),capability_report=CAST(:report AS jsonb),transient_failures=0,
                failure_times='{}',window_start=clock_timestamp(),probe_token=NULL,probe_until=NULL,
                open_until=CASE WHEN :passed THEN NULL ELSE clock_timestamp()+interval '30 seconds' END
                WHERE model_key=:key AND state='half_open' AND probe_token=:token AND probe_until>clock_timestamp()"""),
                {
                    "key": lease.model_key,
                    "token": lease.token,
                    "state": "closed" if capabilities.passed else "open",
                    "passed": capabilities.passed,
                    "report": canonical_json(report),
                },
            )
            return result.rowcount == 1

    async def record_attempt(
        self, workspace: UUID, attempt: UUID, config_sha256: str, endpoint_sha256: str
    ) -> None:
        """One settled ledger row contributes at most one failure, even after worker restart."""
        async with self.db.begin() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT * FROM app.claim_provider_health_outcome(:w,:id)"),
                        {"w": workspace, "id": attempt},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["price_version"] != config_sha256:
                return
            health = (
                (
                    await conn.execute(
                        text(
                            "SELECT *,clock_timestamp() AS now FROM app.provider_health WHERE model_key=:key FOR UPDATE"
                        ),
                        {"key": row["model_key"]},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if health is None or health["state"] in ("disabled", "half_open"):
                return
            report = health["capability_report"]
            if (
                report.get("config_sha256") != config_sha256
                or report.get("endpoint_sha256") != endpoint_sha256
            ):
                return
            transient = row["failure_retryable"] or row["failure_code"] in (
                "provider_transport_interrupted",
                "provider_missing_terminal",
            )
            if not transient:
                return
            now = health["now"]
            times = [
                stamp for stamp in health["failure_times"] if stamp > now - timedelta(seconds=60)
            ]
            times = (times + [now])[-5:]
            opened = len(times) >= 5
            await conn.execute(
                text("""UPDATE app.provider_health SET failure_times=:times,transient_failures=:n,window_start=:first,
                state=CASE WHEN :opened THEN 'open' ELSE state END,
                open_until=CASE WHEN :opened THEN clock_timestamp()+interval '30 seconds' ELSE open_until END
                WHERE model_key=:key"""),
                {
                    "key": row["model_key"],
                    "times": times,
                    "n": len(times),
                    "first": times[0],
                    "opened": opened,
                },
            )

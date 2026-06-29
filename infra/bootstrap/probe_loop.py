"""Local-only periodic readiness probes; never enables hosted generation or paid probes."""

import argparse
import asyncio
import contextlib
import json
import os
import signal
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evidencedesk.workers.config import ProbeSettings

INTERVAL_SECONDS = 30
PROBE_TIMEOUT_SECONDS = 85
STATUS_MAX_AGE_SECONDS = 120


async def readiness(settings: ProbeSettings) -> tuple[bool, datetime | None]:
    """A skipped/busy probe is not a success; inspect authoritative fresh readiness."""
    from evidencedesk.db.model_repository import health
    from evidencedesk.db.session import engine
    from evidencedesk.models.catalog import ModelCatalog, endpoint_sha256

    catalog = ModelCatalog(settings)
    model = next(model for model in catalog.registry if model.key == "local_standard")
    db = engine(settings.dispatch_database_url, pool_size=1)
    try:
        async with db.connect() as conn:
            row = (await health(conn)).get(model.key, {})
        report = row.get("capability_report", {})
        ready = bool(
            model.adapter == "ollama"
            and model.privacy_class == "private"
            and row.get("state") == "closed"
            and row.get("fresh")
            and report.get("passed") is True
            and report.get("config_sha256") == model.fingerprint
            and report.get("endpoint_sha256") == endpoint_sha256(model, settings)
        )
        last_probe = row.get("last_probe_at")
        return ready, last_probe + timedelta(minutes=5) if ready and last_probe else None
    finally:
        await db.dispose()


def write_status(path: Path, *, ready: bool, valid_until: datetime | None) -> None:
    value = {
        "model_key": "local_standard",
        "checked_at": datetime.now(UTC).isoformat(),
        "ready": ready,
        "valid_until": valid_until.isoformat() if valid_until else None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def healthy(path: Path, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    try:
        value: dict[str, Any] = json.loads(path.read_text())
        checked = datetime.fromisoformat(value["checked_at"])
        valid_until = datetime.fromisoformat(value["valid_until"])
        return bool(
            value["model_key"] == "local_standard"
            and value["ready"] is True
            and timedelta(0) <= now - checked < timedelta(seconds=STATUS_MAX_AGE_SECONDS)
            and now < valid_until
        )
    except OSError, ValueError, TypeError, KeyError:
        return False


async def iteration(settings: ProbeSettings, status_path: Path) -> None:
    from evidencedesk.workers.probe import probe_model

    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            # No override/model selector can route this loop to a hosted provider.
            await probe_model(
                "local_standard", allow_paid=False, max_cost_micro_usd=0, settings=settings
            )
    except Exception:
        print("local probe attempt unavailable", flush=True)
    ready, valid_until = False, None
    try:
        async with asyncio.timeout(5):
            ready, valid_until = await readiness(settings)
    except Exception:
        print("local probe readiness unavailable", flush=True)
    write_status(status_path, ready=ready, valid_until=valid_until)
    print(json.dumps({"local_model_ready": ready}), flush=True)


async def run_loop(status_path: Path) -> None:
    from evidencedesk.workers.config import ProbeSettings

    settings = ProbeSettings()  # type: ignore[call-arg]
    stop = asyncio.Event()
    active: asyncio.Task[None] | None = None

    def shutdown() -> None:
        stop.set()
        if active is not None:
            active.cancel()

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, shutdown)
    try:
        write_status(status_path, ready=False, valid_until=None)
        while not stop.is_set():
            active = asyncio.create_task(iteration(settings, status_path))
            try:
                await active
            except asyncio.CancelledError:
                if not stop.is_set():
                    raise
            finally:
                active = None
            if not stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=INTERVAL_SECONDS)
    finally:
        status_path.unlink(missing_ok=True)
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-file", type=Path, default=Path("/tmp/local-model-readiness.json"))
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        raise SystemExit(0 if healthy(args.status_file) else 1)
    os.umask(0o077)
    asyncio.run(run_loop(args.status_file))


if __name__ == "__main__":
    main()

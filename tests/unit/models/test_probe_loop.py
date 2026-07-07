import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from evidencedesk.workers import probe as worker_probe

from infra.bootstrap import probe_loop


async def test_skipped_probe_does_not_report_ready_without_authoritative_readiness(
    tmp_path, monkeypatch
):
    probe = AsyncMock(return_value=0)
    monkeypatch.setattr(worker_probe, "probe_model", probe)
    monkeypatch.setattr(probe_loop, "readiness", AsyncMock(return_value=(False, None)))
    path = tmp_path / "status.json"
    settings = object()
    await probe_loop.iteration(settings, path)
    assert not probe_loop.healthy(path)
    probe.assert_awaited_once_with(
        "local_standard", allow_paid=False, max_cost_micro_usd=0, settings=settings
    )


async def test_busy_local_generation_can_keep_previously_valid_readiness(tmp_path, monkeypatch):
    monkeypatch.setattr(
        worker_probe, "probe_model", AsyncMock(side_effect=RuntimeError("secret exception"))
    )
    monkeypatch.setattr(
        probe_loop,
        "readiness",
        AsyncMock(return_value=(True, datetime.now(UTC) + timedelta(minutes=1))),
    )
    path = tmp_path / "status.json"
    await probe_loop.iteration(object(), path)
    assert probe_loop.healthy(path)


async def test_cancellation_propagates_without_marking_probe_as_success(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_probe, "probe_model", AsyncMock(side_effect=asyncio.CancelledError))
    path = tmp_path / "status.json"
    with pytest.raises(asyncio.CancelledError):
        await probe_loop.iteration(object(), path)
    assert not path.exists()


def test_healthcheck_expires_on_catalog_expiry_or_stalled_loop(tmp_path):
    path = tmp_path / "status.json"
    now = datetime.now(UTC)
    probe_loop.write_status(path, ready=True, valid_until=now + timedelta(seconds=20))
    assert probe_loop.healthy(path)
    assert not probe_loop.healthy(path, now=now + timedelta(seconds=21))
    probe_loop.write_status(path, ready=True, valid_until=now + timedelta(minutes=5))
    assert not probe_loop.healthy(path, now=now + timedelta(seconds=121))
    assert not probe_loop.healthy(path, now=now - timedelta(seconds=1))
    value = json.loads(path.read_text())
    value["ready"] = "true"
    path.write_text(json.dumps(value))
    assert not probe_loop.healthy(path)


def test_healthcheck_needs_no_application_or_ml_imports(tmp_path):
    path = tmp_path / "status.json"
    probe_loop.write_status(path, ready=True, valid_until=datetime.now(UTC) + timedelta(minutes=1))
    checked = subprocess.run(
        [
            sys.executable,
            "-S",
            "infra/bootstrap/probe_loop.py",
            "--healthcheck",
            "--status-file",
            str(path),
        ],
        capture_output=True,
        timeout=3,
    )
    assert checked.returncode == 0, checked.stderr.decode()

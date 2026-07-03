import json
from pathlib import Path

import httpx2
import psycopg
import pytest
from evidencedesk.providers.openai import OpenAIAdapter
from evidencedesk.workers.config import ProbeSettings
from evidencedesk.workers.probe import probe_model

from tests.contracts.providers.test_openai import ANSWER, Bytes, completed, delta

pytestmark = pytest.mark.integration


async def test_operator_probe_requires_opt_in_reserves_before_native_io_and_persists_readiness(
    database, tmp_path, monkeypatch
):
    key = tmp_path / "probe-key"
    key.write_text("fixture-secret")
    settings = ProbeSettings(
        database_url=database["ed_app"],
        dispatch_database_url=database["ed_dispatch"],
        openai_key_file=key,
        model_config_path=Path("contracts/config/models.yaml"),
    )
    wires = []

    def handler(request):
        with psycopg.connect(database["operator"]) as conn:
            state, reserved = conn.execute(
                "SELECT state,probe_reserved_micro_usd FROM app.provider_health WHERE model_key='paid_standard'"
            ).fetchone()
            assert state == "half_open" and reserved > 0
        raw = json.dumps(ANSWER)
        wire = Bytes(
            "".join(
                "data: " + json.dumps(e) + "\n\n" for e in [delta(raw), completed(raw)]
            ).encode()
        )
        wires.append(wire)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    def native(key, **kwargs):
        return OpenAIAdapter(key, transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr("evidencedesk.workers.probe.OpenAIAdapter", native)
    with pytest.raises(ValueError, match="opt-in"):
        await probe_model("paid_standard", settings=settings)
    with pytest.raises(ValueError, match="quote"):
        await probe_model("paid_standard", settings=settings, allow_paid=True, max_cost_micro_usd=1)
    assert not wires
    with psycopg.connect(database["operator"]) as conn:
        conn.execute("DELETE FROM app.provider_health WHERE model_key='paid_standard'")
    try:
        assert (
            await probe_model(
                "paid_standard", settings=settings, allow_paid=True, max_cost_micro_usd=10000
            )
            == 0
        )
        assert len(wires) == 2 and all(w.closed for w in wires)
        with psycopg.connect(database["operator"]) as conn:
            row = conn.execute(
                "SELECT state,capability_report,probe_reserved_micro_usd FROM app.provider_health WHERE model_key='paid_standard'"
            ).fetchone()
            assert row[0] == "closed" and row[1]["passed"]
            assert all(row[1]["capabilities"].values()) and row[2] > 0
        assert (
            await probe_model(
                "paid_standard", settings=settings, allow_paid=True, max_cost_micro_usd=10000
            )
            == 0
        )
        assert len(wires) == 2  # Fresh probe does not spend again.
    finally:
        with psycopg.connect(database["operator"]) as conn:
            conn.execute("DELETE FROM app.provider_health WHERE model_key='paid_standard'")

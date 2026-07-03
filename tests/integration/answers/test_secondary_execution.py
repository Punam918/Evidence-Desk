import hashlib
import json

import httpx2
import psycopg
import pytest
import yaml
from evidencedesk.ai.orchestrator import ModelBinding
from evidencedesk.ai.token_budget import context_packer
from evidencedesk.models.catalog import ModelCatalog
from evidencedesk.providers.anthropic import AnthropicAdapter
from evidencedesk.providers.google import GoogleAdapter
from evidencedesk.workers.config import ProbeSettings
from evidencedesk.workers.probe import probe_model

from tests.contracts.providers.test_openai import Bytes
from tests.contracts.providers.test_secondary import anthropic_events, google_events
from tests.integration.answers.test_execution import ANSWER
from tests.integration.answers.test_execution import grounded as grounded

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("provider", ["anthropic", "google"])
async def test_secondary_requires_operator_enable_probe_and_native_grounded_execution(
    grounded, database, tmp_path, monkeypatch, provider
):
    key = provider + "_candidate"
    current = grounded.client.get(grounded.base + "/models").json()["items"]
    assert next(m for m in current if m["key"] == key)["available"] is False
    settings = grounded.client.app.state.settings
    config = yaml.safe_load(settings.model_config_path.read_text())
    for candidate in config["models"]:
        if candidate["key"] == key:
            candidate["enabled_after_conformance"] = True
    registry = tmp_path / (provider + "-models.yaml")
    registry.write_text(yaml.safe_dump(config))
    secret = tmp_path / (provider + "-key")
    secret.write_text("fixture-secret")
    setattr(settings, provider + "_key_file", secret)
    settings.model_config_path = registry
    catalog = ModelCatalog(settings)
    grounded.client.app.state.models.registry = catalog.registry
    grounded.client.app.state.chat.models.registry = catalog.registry
    grounded.client.app.state.chat.config_hash = hashlib.sha256(
        settings.policy_config_path.read_bytes() + registry.read_bytes()
    ).hexdigest()
    current = grounded.client.get(grounded.base + "/models").json()["items"]
    assert next(m for m in current if m["key"] == key)["available"] is False
    wires = []

    def handler(request):
        events = (anthropic_events if provider == "anthropic" else google_events)(
            json.dumps(ANSWER)
        )
        payload = "".join(
            ("event: " + e["type"] + "\n" if provider == "anthropic" else "")
            + "data: "
            + json.dumps(e)
            + "\n\n"
            for e in events
        ).encode()
        wire = Bytes(payload, step=30)
        wires.append(wire)
        with psycopg.connect(database["operator"]) as conn:
            row = conn.execute(
                "SELECT state,probe_reserved_micro_usd FROM app.provider_health WHERE model_key=%s",
                (key,),
            ).fetchone()
            assert row and row[1] > 0
            if row[0] == "closed":
                assert conn.execute(
                    "SELECT count(*) FROM app.model_usage WHERE model_key=%s AND execution_state='issued' AND state='reserved'",
                    (key,),
                ).fetchone() == (1,)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    native = AnthropicAdapter if provider == "anthropic" else GoogleAdapter
    probe_settings = ProbeSettings(
        database_url=database["ed_app"],
        dispatch_database_url=database["ed_dispatch"],
        model_config_path=registry,
        **{provider + "_key_file": secret},
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            "evidencedesk.providers." + provider + "." + native.__name__,
            lambda key: native(key, transport=httpx2.MockTransport(handler)),
        )
        assert (
            await probe_model(
                key, settings=probe_settings, allow_paid=True, max_cost_micro_usd=10000
            )
            == 0
        )
    assert len(wires) == 2 and all(w.closed for w in wires)
    current = grounded.client.get(grounded.base + "/models").json()["items"]
    assert next(m for m in current if m["key"] == key)["available"] is True
    model = next(m for m in catalog.registry if m.key == key)
    adapter = native("fixture-secret", transport=httpx2.MockTransport(handler))
    binding = ModelBinding(model, adapter, context_packer(model, adapter))

    def factory(ctx, lease):
        execution = grounded.factory(ctx, lease)
        execution.catalog = catalog
        execution.bindings = {**execution.bindings, key: binding}
        return execution

    try:
        run = grounded.admit(model_key=key)
        await grounded.execute(factory)
        assert grounded.row(run)[0] == "completed", grounded.row(run)
        assert len(wires) == 3 and all(w.closed for w in wires)
        with psycopg.connect(database["operator"]) as conn:
            row = conn.execute(
                "SELECT model_key,input_tokens,output_tokens,cached_input_tokens,reasoning_output_tokens,state FROM app.model_usage WHERE run_id=%s",
                (run["id"],),
            ).fetchone()
            assert row == (key, 100, 20, 30, 5, "settled")
        setattr(settings, provider + "_key_file", tmp_path / "missing-key")
        current = grounded.client.get(grounded.base + "/models").json()["items"]
        assert next(m for m in current if m["key"] == key)["available"] is False
    finally:
        await adapter.close()

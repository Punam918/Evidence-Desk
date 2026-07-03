from uuid import uuid4

import psycopg
import pytest
from evidencedesk.db.model_repository import health
from evidencedesk.db.session import engine
from evidencedesk.models.catalog import ModelCatalog, endpoint_sha256
from evidencedesk.models.health import Capabilities, ProviderHealth
from evidencedesk.workers.config import ProbeSettings

from infra.bootstrap.probe_loop import readiness

pytestmark = pytest.mark.integration


async def test_probe_refresh_is_due_before_serving_freshness_expires(database):
    db = engine(database["ed_dispatch"])
    key = "refresh_" + uuid4().hex
    probes = ProviderHealth(db)
    try:
        lease = await probes.claim_probe(key, "a" * 64, "b" * 64)
        assert lease and await probes.complete_probe(lease, Capabilities(True, True, True, True))
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.provider_health SET last_probe_at=clock_timestamp()-interval '235 seconds' WHERE model_key=%s",
                (key,),
            )
        assert await probes.claim_probe(key, "a" * 64, "b" * 64) is None
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.provider_health SET last_probe_at=clock_timestamp()-interval '245 seconds' WHERE model_key=%s",
                (key,),
            )
        async with db.connect() as conn:
            assert (await health(conn))[key]["fresh"] is True
        refreshed = await probes.claim_probe(key, "a" * 64, "b" * 64)
        assert refreshed
        assert await probes.claim_probe(key, "a" * 64, "b" * 64) is None
        assert await probes.complete_probe(refreshed, Capabilities(True, True, True, True))
        async with db.connect() as conn:
            row = (await health(conn))[key]
            assert row["fresh"] is True and row["state"] == "closed"
    finally:
        with psycopg.connect(database["operator"]) as conn:
            conn.execute("DELETE FROM app.provider_health WHERE model_key=%s", (key,))
        await db.dispose()


async def test_loop_readiness_uses_current_local_model_report_and_expiry(database):
    settings = ProbeSettings(
        database_url=database["ed_app"],
        dispatch_database_url=database["ed_dispatch"],
        local_model_url="http://ollama:11434",
    )
    model = next(
        model for model in ModelCatalog(settings).registry if model.key == "local_standard"
    )
    db = engine(database["ed_dispatch"])
    probes = ProviderHealth(db)
    try:
        assert await readiness(settings) == (False, None)
        lease = await probes.claim_probe(
            model.key, model.fingerprint, endpoint_sha256(model, settings)
        )
        assert lease and await probes.complete_probe(lease, Capabilities(True, True, True, True))
        ready, expiry = await readiness(settings)
        assert ready and expiry is not None
        changed = settings.model_copy(update={"local_model_url": "http://127.0.0.1:23456"})
        assert await readiness(changed) == (False, None)
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.provider_health SET last_probe_at=clock_timestamp()-interval '301 seconds' WHERE model_key=%s",
                (model.key,),
            )
        assert await readiness(settings) == (False, None)
    finally:
        with psycopg.connect(database["operator"]) as conn:
            conn.execute("DELETE FROM app.provider_health WHERE model_key=%s", (model.key,))
        await db.dispose()

"""Explicit one-shot operator probe; no chat-worker startup can silently spend on probes."""

from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, datetime

from evidencedesk.db.model_slots import local_generation_slot
from evidencedesk.db.session import engine
from evidencedesk.models.catalog import ModelCatalog, endpoint_sha256
from evidencedesk.models.health import Capabilities, ProviderHealth
from evidencedesk.providers.base import ClosableModelAdapter
from evidencedesk.providers.local_tokens import LocalTokens
from evidencedesk.providers.ollama import OllamaAdapter
from evidencedesk.providers.openai import OpenAIAdapter
from evidencedesk.providers.probe import probe, probe_reservation
from evidencedesk.workers.config import ProbeSettings, secret
from evidencedesk.workers.embedding import cpu_call
from evidencedesk.workers.runtime import event


async def probe_model(
    key: str,
    *,
    allow_paid: bool = False,
    max_cost_micro_usd: int = 0,
    settings: ProbeSettings | None = None,
) -> int:
    settings = settings or ProbeSettings()  # type: ignore[call-arg]
    catalog = ModelCatalog(settings)
    model = next((m for m in catalog.registry if m.key == key), None)
    if (
        model is None
        or not model.enabled_after_conformance
        or (model.price_valid_until and model.price_valid_until < datetime.now(UTC).date())
        or not 0 <= max_cost_micro_usd <= 10_000
    ):
        raise ValueError("Probe selection or maximum operator cost is unavailable")
    hosted = model.privacy_class == "hosted"
    if hosted and (not allow_paid or max_cost_micro_usd == 0):
        raise ValueError("Hosted probes require explicit opt-in and a positive per-invocation cap")
    adapter: ClosableModelAdapter
    if model.adapter == "openai" and settings.openai_key_file:
        adapter = OpenAIAdapter(
            secret(settings.openai_key_file), tokenizer_cache=settings.model_cache_path
        )
    elif model.adapter == "anthropic" and settings.anthropic_key_file:
        from evidencedesk.providers.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(secret(settings.anthropic_key_file))
    elif model.adapter == "google" and settings.google_key_file:
        from evidencedesk.providers.google import GoogleAdapter

        adapter = GoogleAdapter(secret(settings.google_key_file))
    elif model.adapter == "ollama" and settings.local_model_url and model.resolved_digest:
        tokens = await cpu_call(
            LocalTokens,
            settings.model_cache_path / "qwen3-tokenizer",
            settings.local_tokenizer_lock,
        )
        adapter = OllamaAdapter(
            settings.local_model_url,
            model.model_id,
            model.resolved_digest,
            tokens,
            api_key=secret(settings.local_model_key_file)
            if settings.local_model_key_file
            else None,
        )
    else:
        raise ValueError("No configured native probe transport")
    db, dispatch = engine(settings.database_url), engine(settings.dispatch_database_url)
    health = ProviderHealth(dispatch)
    try:
        amount = await cpu_call(probe_reservation, adapter, model)
        if amount > max_cost_micro_usd:
            raise ValueError("Worst-case two-call probe quote exceeds the operator cap")
        # Local probe and serving inference share the same global serialization slot.
        async with local_generation_slot(db) if not hosted else nullcontext():
            lease = await health.claim_probe(
                key, model.fingerprint, endpoint_sha256(model, settings), reserved_micro_usd=amount
            )
            if lease is None:
                event("probe_not_due")
                return 0
            capabilities = Capabilities(False, False, False, False)
            try:
                result = await probe(adapter, model)
                capabilities = Capabilities(**asdict(result))
            except Exception:
                # No raw upstream exception, query, secret, response or endpoint in logs.
                event("probe_failed")
            finally:
                committed = await health.complete_probe(lease, capabilities)
            event("probe_passed" if committed and capabilities.passed else "probe_unavailable")
            return 0 if committed and capabilities.passed else 1
    finally:
        await adapter.close()
        await db.dispose()
        await dispatch.dispose()

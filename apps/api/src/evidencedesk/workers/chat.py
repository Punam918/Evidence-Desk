"""Chat worker lifecycle: warm local models before claims, then execute the durable graph."""

import hashlib
import time
from uuid import uuid4

from evidencedesk.ai.orchestrator import AnswerExecution, ModelBinding
from evidencedesk.ai.token_budget import context_packer
from evidencedesk.chat.query_limits import QueryLimits
from evidencedesk.db.identity_repository import IdentityRepository
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.model_slots import local_generation_slot
from evidencedesk.db.session import engine
from evidencedesk.embeddings.encoder import Encoder
from evidencedesk.errors import Problem
from evidencedesk.index.qdrant import IndexConfig, client
from evidencedesk.jobs.models import Lease
from evidencedesk.models.catalog import ModelCatalog
from evidencedesk.models.health import ProviderHealth
from evidencedesk.providers.base import ClosableModelAdapter
from evidencedesk.providers.local_tokens import LocalTokens
from evidencedesk.providers.ollama import OllamaAdapter
from evidencedesk.providers.openai import OpenAIAdapter
from evidencedesk.retrieval.index import SearchIndex
from evidencedesk.retrieval.rerank import Reranker
from evidencedesk.retrieval.rerank_process import RerankProcess, RerankUnavailable
from evidencedesk.retrieval.service import Retriever
from evidencedesk.workers.config import ChatSettings, secret
from evidencedesk.workers.embedding import cpu_call
from evidencedesk.workers.runtime import consume, event
from evidencedesk.workflows.runner import WorkflowRunner


async def chat(once: bool, settings: ChatSettings | None = None) -> int:
    settings = settings or ChatSettings()  # type: ignore[call-arg]  # Mounted worker environment.
    db, dispatch, identity_db = (
        engine(settings.database_url),
        engine(settings.dispatch_database_url),
        engine(settings.identity_database_url),
    )
    index = client(settings.qdrant_url, secret(settings.qdrant_read_key_file))
    scorer = RerankProcess(settings.model_cache_path, settings.model_artifact_lock)
    adapters: list[ClosableModelAdapter] = []
    try:
        catalog = ModelCatalog(settings)
        bindings = {}
        for model in catalog.registry:
            if not model.enabled_after_conformance:
                continue
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
                continue
            adapters.append(adapter)
            bindings[model.key] = ModelBinding(
                model, adapter, await cpu_call(context_packer, model, adapter)
            )
        if not bindings:
            raise ValueError("No core model transport is configured")
        encoder = await cpu_call(Encoder, settings.model_cache_path, settings.model_artifact_lock)
        config = IndexConfig(settings.index_config_path)
        retriever = Retriever(
            db,
            SearchIndex(index),
            encoder,
            config=config,
            limits=QueryLimits(settings.model_cache_path, settings.model_artifact_lock),
        )
        reranker = Reranker(db, scorer, config)
        jobs = JobRepository(dispatch)
        runner = WorkflowRunner(
            db, IdentityRepository(identity_db), jobs, settings.checkpoint_database_url
        )
        config_hash = hashlib.sha256(
            settings.policy_config_path.read_bytes() + settings.model_config_path.read_bytes()
        ).hexdigest()
        worker = "chat-" + uuid4().hex
        next_warm = 0.0

        async def claim() -> Lease | None:
            nonlocal next_warm
            if (
                scorer.process is None or scorer.process.returncode is not None
            ) and time.monotonic() >= next_warm:
                next_warm = time.monotonic() + 30
                try:
                    async with local_generation_slot(db, slot_id=3):
                        await scorer.start()
                except RerankUnavailable, Problem, ValueError, OSError, TimeoutError:
                    event("reranker_unavailable")
            return await jobs.claim("chat", worker)

        async def run(lease: Lease) -> None:
            await runner.execute(
                lease,
                uuid4(),
                lambda ctx, claimed: AnswerExecution(
                    db,
                    ctx,
                    claimed,
                    retriever,
                    reranker,
                    catalog,
                    bindings,
                    config_hash,
                    ProviderHealth(dispatch),
                ),
            )

        return await consume(claim, run, jobs.sweep, once=once)
    finally:
        await scorer.close()
        for adapter in adapters:
            await adapter.close()
        await index.close()
        await db.dispose()
        await dispatch.dispose()
        await identity_db.dispose()

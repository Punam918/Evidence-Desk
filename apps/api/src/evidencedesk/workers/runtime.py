"""Process composition only. Domain modules own authorization, transactions and recovery."""

import asyncio
import hashlib
import json
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from evidencedesk.db.index_repository import OutboxRepository
from evidencedesk.db.jobs_repository import JobRepository
from evidencedesk.db.session import engine
from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.index.qdrant import IndexConfig, IndexWriter, client
from evidencedesk.observability.stages import event as metric_event
from evidencedesk.observability.stages import stage
from evidencedesk.workers.config import IndexSettings, IngestionSettings, secret


def event(name: str, **values: Any) -> None:
    fields = {
        "worker_lease_lost": frozenset(),
        "worker_operation_failed": frozenset({"status", "retryable"}),
        "index_orphans": frozenset({"workspace_id", "removed", "next_point"}),
        "index_audit": frozenset(
            {"workspace_id", "checked", "repairs", "failures", "next_revision"}
        ),
    }
    if name not in fields:
        return
    record: dict[str, str | int | bool | None] = {
        "event": name,
        "timestamp_unix_ns": time.time_ns(),
    }
    for key in fields[name]:
        value = values.get(key)
        if key in {"workspace_id", "next_point", "next_revision"}:
            if isinstance(value, UUID):
                record[key] = str(value)
        elif type(value) is bool or (type(value) is int and 0 <= value <= 1_000_000_000):
            record[key] = value
    print(json.dumps(record), flush=True)


async def consume[T](
    claim: Callable[[], Awaitable[T | None]],
    run: Callable[[T], Awaitable[Any]],
    sweep: Callable[[], Awaitable[int]],
    *,
    once: bool,
) -> int:
    next_sweep = 0.0
    while True:
        if time.monotonic() >= next_sweep:
            with stage("worker.sweep"):
                await sweep()
            next_sweep = time.monotonic() + 10
        with stage("worker.claim"):
            lease = await claim()
        failed = False
        if lease:
            try:
                with stage("worker.job"):
                    await run(lease)
            except LeaseLost:
                metric_event("worker.lease_lost")
                event("worker_lease_lost")
            except Problem as exc:
                failed = True
                metric_event("worker.failure")
                event("worker_operation_failed", status=exc.status, retryable=exc.retryable)
        if once:
            return int(failed)
        if lease is None:
            await asyncio.sleep(0.5)


def parser_image(path: Path) -> str:
    lock = json.loads(path.read_text())
    for filename, sha in lock["files"].items():
        source = (
            Path("apps/api/src") / filename
            if filename.startswith("evidencedesk/")
            else Path("infra/parser") / filename
        )
        if hashlib.sha256(source.read_bytes()).hexdigest() != sha:
            raise ValueError("Parser build inputs differ from the frozen local image")
    return str(lock["image_id"])


async def ingestion(once: bool, settings: IngestionSettings | None = None) -> int:
    from evidencedesk.embeddings.encoder import Encoder
    from evidencedesk.ingestion.parser_client import SocketParserSandbox
    from evidencedesk.ingestion.publish import Publisher
    from evidencedesk.ingestion.sandbox import Parser, ParserSandbox
    from evidencedesk.workers.embedding import EmbeddingWorker, cpu_call
    from evidencedesk.workers.ingest import ExtractionWorker
    from evidencedesk.workers.pipeline import IngestionPipeline

    settings = settings or IngestionSettings()  # type: ignore[call-arg]  # BaseSettings resolves required environment fields.
    db, dispatch = engine(settings.database_url), engine(settings.dispatch_database_url)
    store = settings.storage()
    try:
        jobs = JobRepository(dispatch)
        encoder = await cpu_call(Encoder, settings.model_cache_path, settings.model_artifact_lock)
        parser: Parser = (
            SocketParserSandbox(settings.parser_socket_path, settings.parser_service_lock)
            if settings.parser_socket_path is not None
            else ParserSandbox(parser_image(settings.parser_image_lock))
        )
        pipeline = IngestionPipeline(
            ExtractionWorker(db, jobs, store, parser),
            EmbeddingWorker(db, jobs, store, encoder),
            Publisher(db, jobs, store, IndexConfig(settings.index_config_path)),
        )
        worker = "ingest-" + uuid4().hex
        return await consume(
            lambda: jobs.claim("ingest", worker), pipeline.run, jobs.sweep, once=once
        )
    finally:
        store.close()
        await db.dispose()
        await dispatch.dispose()


async def indexing(once: bool, settings: IndexSettings | None = None) -> int:
    from evidencedesk.workers.index import IndexWorker

    settings = settings or IndexSettings()  # type: ignore[call-arg]  # BaseSettings resolves required environment fields.
    db, dispatch = engine(settings.database_url), engine(settings.dispatch_database_url)
    store = settings.storage()
    connection = client(settings.qdrant_url, secret(settings.qdrant_write_key_file))
    try:
        writer = IndexWriter(connection, IndexConfig(settings.index_config_path))
        await writer.provision()
        outbox = OutboxRepository(dispatch)
        worker = IndexWorker(db, outbox, store, writer)
        return await consume(outbox.claim, worker.run, JobRepository(dispatch).sweep, once=once)
    finally:
        await connection.close()
        store.close()
        await db.dispose()
        await dispatch.dispose()


async def reconcile(
    workspace: UUID,
    after: UUID | None,
    settings: IndexSettings | None = None,
    *,
    orphans: bool = False,
) -> int:
    from evidencedesk.lifecycle.reconcile import Reconciler

    settings = settings or IndexSettings()  # type: ignore[call-arg]  # BaseSettings resolves required environment fields.
    db, dispatch = engine(settings.database_url), engine(settings.dispatch_database_url)
    store = settings.storage()
    connection = client(settings.qdrant_url, secret(settings.qdrant_write_key_file))
    try:
        worker = Reconciler(
            db,
            OutboxRepository(dispatch),
            store,
            IndexWriter(connection, IndexConfig(settings.index_config_path)),
        )
        if orphans:
            removed, offset = await worker.orphans(workspace, str(after) if after else None)
            event("index_orphans", workspace_id=workspace, removed=removed, next_point=offset)
            return 0
        report = await worker.audit_workspace(workspace, after)
        event("index_audit", workspace_id=workspace, **asdict(report))
        return int(report.failures > 0)
    finally:
        await connection.close()
        store.close()
        await db.dispose()
        await dispatch.dispose()


async def with_signals(operation: Awaitable[int]) -> int:
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        return await operation
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)

"""Explicit code-owned instrumentation; arguments and exceptions never become attributes."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from evidencedesk.observability.telemetry import Telemetry

current: ContextVar[Telemetry | None] = ContextVar("ed_telemetry", default=None)
STAGES = frozenset(
    {
        "worker.job",
        "worker.claim",
        "worker.sweep",
        "retrieval.total",
        "retrieval.query",
        "retrieval.embed_query",
        "retrieval.rerank",
        "ingest.parse",
        "ingest.embed",
        "ingest.publish",
        "index.write",
        "index.reconcile",
        "checkpoint.read",
        "checkpoint.delete",
        "checkpoint.write",
        "checkpoint.pending_write",
        "model.attempt",
        *[
            "answer." + node
            for node in (
                "authorize",
                "route",
                "retrieve",
                "context",
                "generate",
                "validate",
                "finalize",
                "abstain",
                "terminalize",
            )
        ],
        *["brief." + node for node in ("prepare", "approval", "reauthorize", "save")],
    }
)
EVENTS = frozenset(
    {
        "graph.replay",
        "graph.interrupt",
        "checkpoint.failure",
        "worker.lease_lost",
        "worker.failure",
        "retrieval.degraded",
        "retrieval.candidate_filtered",
        "schema.invalid",
    }
)


def event(name: str) -> None:
    telemetry = current.get()
    if telemetry is not None and name in EVENTS:
        try:
            telemetry.operations.labels(name).inc()
        except Exception:
            pass


@contextmanager
def stage(name: str) -> Iterator[None]:
    telemetry = current.get()
    if telemetry is None or name not in STAGES:
        yield
        return
    started_ns, started = time.time_ns(), time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException as exc:
        outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
        if name.startswith("checkpoint."):
            event("checkpoint.failure")
        raise
    finally:
        telemetry.record_stage(name, outcome, started_ns, time.perf_counter() - started)


def instrument[**P, T](
    name: str,
) -> Callable[[Callable[P, Coroutine[Any, Any, T]]], Callable[P, Coroutine[Any, Any, T]]]:
    def decorate(function: Callable[P, Awaitable[T]]) -> Callable[P, Coroutine[Any, Any, T]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            with stage(name):
                return await function(*args, **kwargs)

        return wrapped

    return decorate


def retrieval_candidates(dense: int, sparse: int, filtered: bool, degraded: bool) -> None:
    telemetry = current.get()
    if telemetry is None:
        return
    try:
        for branch, count in (("dense", dense), ("sparse", sparse)):
            if type(count) is int and 0 <= count <= 160:
                telemetry.candidates.labels(branch).observe(count)
        if filtered:
            event("retrieval.candidate_filtered")
        if degraded:
            event("retrieval.degraded")
    except Exception:
        pass

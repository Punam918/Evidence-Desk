"""Retry coordination reads the settled ledger, so graph replay never invents another attempt."""

import asyncio
import random
from contextlib import aclosing
from typing import TYPE_CHECKING
from uuid import UUID

from evidencedesk.ai.manifest import PromptManifest
from evidencedesk.ai.router import AttemptOutcome, RetryDecision, RoutePlan, next_attempt
from evidencedesk.db import answer_repository as repository
from evidencedesk.db import workflow_repository
from evidencedesk.db.session import authorized_transaction
from evidencedesk.errors import Problem
from evidencedesk.events.models import Phase
from evidencedesk.providers.base import ModelFault, ModelResult

if TYPE_CHECKING:
    from evidencedesk.ai.orchestrator import AnswerExecution


def retry_jitter() -> float:
    return random.SystemRandom().random()


async def generate(execution: AnswerExecution) -> tuple[UUID, ModelResult] | None:
    e = execution
    while True:
        run = await e.read()
        manifest = run.manifest
        if manifest is None:
            raise Problem(409, "invalid_execution_manifest", "Generation prompt is unavailable.")
        if manifest.abstention:
            return None
        async with authorized_transaction(e.db, e.ctx) as conn:
            stored = await repository.stored_result(conn, e.ctx, e.lease)
            attempts = await repository.attempts(conn, e.ctx, e.lease)
            remaining = await workflow_repository.remaining_seconds(conn, e.ctx, e.lease)
        if e.health:
            manifests = [manifest] + [
                PromptManifest.model_validate(raw)
                for raw in run.metadata.get("prior_manifests", [])
            ]
            for attempt in attempts:
                previous = next(
                    (
                        m
                        for m in manifests
                        if m.model_key == attempt.model_key
                        and m.model_sha256 == attempt.price_version
                    ),
                    None,
                )
                if previous and attempt.state != "reserved":
                    await e.health.record_attempt(
                        e.ctx.workspace_id,
                        attempt.id,
                        previous.model_sha256,
                        previous.endpoint_sha256,
                    )
        if stored is not None:
            return stored
        async with authorized_transaction(e.db, e.ctx) as conn:
            choices = await e.catalog.availability(conn, e.ctx)
        eligible = {
            choice.key
            for choice in choices
            if choice.available and choice.supports_schema and choice.key in e.bindings
        }
        if attempts and attempts[-1].execution != "not_issued":
            outcomes = [
                AttemptOutcome(a.model_key, a.fault) for a in attempts if a.fault is not None
            ]
            raw_plan = run.metadata.get("route_plan")
            if not raw_plan or len(outcomes) != len(attempts):
                raise Problem(409, "provider_interrupted", "The prior attempt cannot be resumed.")
            plan = RoutePlan(
                raw_plan["preference"],
                tuple(raw_plan["keys"]),
                tuple(tuple(x) for x in raw_plan["exclusions"]),
            )
            if attempts[-1].model_key != manifest.model_key:
                # A fallback manifest was already committed before the crash; never switch back.
                decision = (
                    RetryDecision("fallback", manifest.model_key, 0, "resume_frozen_fallback")
                    if manifest.model_key in eligible
                    else RetryDecision("stop", None, 0, "model_unavailable")
                )
            else:
                decision = next_attempt(plan, outcomes, eligible, remaining, retry_jitter())
            if decision.action == "stop":
                fault = outcomes[-1].fault
                raise Problem(
                    503,
                    "provider_interrupted" if fault.execution == "ambiguous" else fault.code,
                    "The provider request did not complete.",
                )
            assert decision.model_key is not None
            async with authorized_transaction(e.db, e.ctx, write=True) as conn:
                await repository.metadata(
                    conn,
                    e.ctx,
                    e.lease,
                    {
                        "retry_decision": {
                            "after_attempt": str(attempts[-1].id),
                            "action": decision.action,
                            "key": decision.model_key,
                            "reason": decision.reason,
                        }
                    },
                )
            if decision.delay_seconds:
                await asyncio.sleep(decision.delay_seconds)
            if decision.model_key != manifest.model_key:
                manifest = await e.repack(run, manifest, decision.model_key)
                run = await e.read()
                if manifest.abstention:
                    return None
        # Checks readiness and the frozen endpoint again after any delay/transition.
        binding = await e.route(run)
        bundle = await e.restore(run, manifest)
        assert bundle.request is not None
        await e.events.append(e.ctx, e.lease, Phase(phase="generating", degraded=manifest.degraded))
        async with aclosing(
            e.billing.execute(
                e.ctx,
                e.lease,
                bundle.request,
                binding.adapter,
                binding.model.prices,
                hosted=binding.model.privacy_class == "hosted",
            )
        ) as stream:
            async for event in stream:
                if isinstance(event, (ModelFault, ModelResult)):
                    break
        # Only the committed ledger determines the next action, including crash recovery.

"""Bounded route policy. This module never performs inference or relaxes catalog eligibility."""

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

from evidencedesk.errors import Problem
from evidencedesk.models.registry import ModelAvailability
from evidencedesk.providers.base import ModelFault


@dataclass(frozen=True)
class RoutePlan:
    preference: str
    keys: tuple[str, ...]
    exclusions: tuple[tuple[str, str], ...]


def plan_routes(
    preference: str,
    intent: Literal["ask", "compare"],
    choices: Sequence[ModelAvailability],
    bound_keys: Collection[str],
) -> RoutePlan:
    """Catalog results must be resolved under live workspace authorization by the caller."""
    eligible, exclusions = set(), []
    for choice in choices:
        reason = (
            choice.unavailable_reason or "model_unavailable"
            if not choice.available
            else "schema_unsupported"
            if not choice.supports_schema
            else "adapter_not_configured"
            if choice.key not in bound_keys
            else None
        )
        if reason:
            exclusions.append((choice.key, reason))
        else:
            eligible.add(choice.key)
    ranked = (
        (preference,)
        if preference != "auto"
        else ("paid_quality", "paid_standard", "local_standard")
        if intent == "compare"
        else ("paid_standard", "local_standard")
    )
    keys = tuple(key for key in ranked if key in eligible)[:2]
    if not keys:
        raise Problem(409, "model_unavailable", "No ready model is eligible for this selection.")
    return RoutePlan(preference, keys, tuple(sorted(exclusions)))


@dataclass(frozen=True)
class AttemptOutcome:
    model_key: str
    fault: ModelFault


@dataclass(frozen=True)
class RetryDecision:
    action: Literal["retry", "fallback", "stop"]
    model_key: str | None
    delay_seconds: float
    reason: str


def next_attempt(
    plan: RoutePlan,
    outcomes: Sequence[AttemptOutcome],
    eligible_now: Collection[str],
    remaining_seconds: float,
    jitter_fraction: float,
) -> RetryDecision:
    """One same-route retry and one fallback maximum, only after a definite transient failure.

    A positive decision still requires a fresh context pack, source/policy check and
    transactional reservation. Complete/refused/uncertain results never enter this policy.
    """

    def stop(reason: str) -> RetryDecision:
        return RetryDecision("stop", None, 0, reason)

    if not outcomes or len(outcomes) >= 3:
        return stop("attempt_limit")
    current = outcomes[-1]
    if any(o.fault.execution != "definite_failure" for o in outcomes):
        return stop("uncertain_attempt")
    if not current.fault.retryable:
        return stop("non_retryable")
    if not math.isfinite(remaining_seconds) or remaining_seconds <= 10:
        return stop("deadline_exceeded")
    if not 0 <= jitter_fraction <= 1:
        raise ValueError("Jitter fraction must be between zero and one")
    used = tuple(o.model_key for o in outcomes)
    if any(key not in plan.keys for key in used):
        return stop("invalid_route_history")
    # Only the original route may be retried. The fallback gets one attempt.
    delay = min(4.0, 0.5 * 2 ** len(outcomes)) * jitter_fraction
    retry_after = current.fault.retry_after
    if retry_after is not None:
        if not math.isfinite(retry_after) or retry_after < 0:
            return stop("invalid_retry_after")
        delay = max(delay, retry_after)
    if (
        len(outcomes) == 1
        and current.model_key == plan.keys[0]
        and current.model_key in eligible_now
    ):
        if delay + 10 < remaining_seconds:
            return RetryDecision("retry", current.model_key, delay, "definite_transient_retry")
    if (
        plan.preference == "auto"
        and len(plan.keys) == 2
        and plan.keys[1] not in used
        and plan.keys[1] in eligible_now
    ):
        return RetryDecision("fallback", plan.keys[1], 0, "eligible_auto_fallback")
    return stop("no_eligible_fallback")

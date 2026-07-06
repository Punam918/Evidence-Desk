import pytest
from evidencedesk.ai.router import AttemptOutcome, next_attempt, plan_routes
from evidencedesk.errors import Problem
from evidencedesk.models.registry import ModelAvailability
from evidencedesk.providers.base import ModelFault


def choices(private_only=False):
    return [
        ModelAvailability(
            key=key,
            display_name=key,
            provider="ollama" if key == "local_standard" else "openai",
            privacy_class="private" if key == "local_standard" else "hosted",
            available=not private_only or key == "local_standard",
            supports_schema=True,
            context_cap=8192,
            unavailable_reason="privacy_policy"
            if private_only and key != "local_standard"
            else None,
        )
        for key in ("paid_quality", "paid_standard", "local_standard")
    ]


def test_route_preference_and_live_privacy_are_preserved():
    available = choices()
    keys = [c.key for c in available]
    assert plan_routes("auto", "compare", available, keys).keys == ("paid_quality", "paid_standard")
    assert plan_routes("auto", "ask", available, keys).keys == ("paid_standard", "local_standard")
    assert plan_routes("local_standard", "ask", available, keys).keys == ("local_standard",)
    assert plan_routes("auto", "compare", choices(True), keys).keys == ("local_standard",)
    with pytest.raises(Problem, match="model_unavailable"):
        plan_routes("paid_standard", "ask", choices(True), keys)
    assert plan_routes("auto", "ask", available, ["local_standard"]).keys == ("local_standard",)


@pytest.mark.parametrize(
    "fault",
    [
        ModelFault("timeout", "ambiguous", True),
        ModelFault("invalid_schema", "definite_failure", False),
    ],
)
def test_uncertain_and_nonretryable_failures_never_fallback(fault):
    plan = plan_routes("auto", "ask", choices(), ["paid_standard", "local_standard"])
    assert (
        next_attempt(plan, [AttemptOutcome("paid_standard", fault)], plan.keys, 45, 0.5).action
        == "stop"
    )


def test_retry_fallback_budget_and_current_eligibility():
    plan = plan_routes("auto", "ask", choices(), ["paid_standard", "local_standard"])
    first = AttemptOutcome("paid_standard", ModelFault("rate_limit", "definite_failure", True, 2))
    retry = next_attempt(plan, [first], plan.keys, 50, 0.5)
    assert (retry.action, retry.model_key, retry.delay_seconds) == ("retry", "paid_standard", 2)
    assert next_attempt(plan, [first, first], plan.keys, 40, 0.5).action == "fallback"
    assert next_attempt(plan, [first, first], ["paid_standard"], 40, 0.5).action == "stop"
    assert (
        next_attempt(
            plan, [first, first, AttemptOutcome("local_standard", first.fault)], plan.keys, 30, 0.5
        ).action
        == "stop"
    )
    explicit = plan_routes("paid_standard", "ask", choices(), plan.keys)
    assert next_attempt(explicit, [first, first], plan.keys, 40, 0.5).action == "stop"
    assert next_attempt(plan, [first], plan.keys, 5, 0.5).action == "stop"
    # An open primary breaker skips its retry; it never changes explicit preference.
    assert next_attempt(plan, [first], ["local_standard"], 40, 0.5).action == "fallback"

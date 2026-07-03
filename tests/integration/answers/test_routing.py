import json

import psycopg
import pytest
from evidencedesk.ai.orchestrator import ModelBinding
from evidencedesk.ai.token_budget import context_packer
from evidencedesk.chat.models import Answer
from evidencedesk.providers.base import ModelFault, ModelResult, Usage
from evidencedesk.providers.openai import OpenAIAdapter

from tests.integration.answers.test_execution import ANSWER
from tests.integration.answers.test_execution import grounded as grounded

pytestmark = pytest.mark.integration


class Planned:
    def __init__(self, native, failures):
        self.native, self.failures = native, failures
        self.calls = []

    def estimate(self, request):
        return self.native.estimate(request)

    async def stream(self, request):
        self.calls.append(request)
        if len(self.calls) <= len(self.failures):
            yield self.failures[len(self.calls) - 1]
        else:
            yield ModelResult(
                "completed",
                Answer.model_validate(ANSWER),
                Usage(input_tokens=100, output_tokens=20),
                "fixture",
                "",
                json.dumps(ANSWER),
            )


@pytest.mark.parametrize("mode", ["retry", "fallback", "explicit", "ambiguous", "nonretryable"])
async def test_durable_retry_and_fallback_policy(grounded, database, monkeypatch, mode):
    monkeypatch.setattr("evidencedesk.ai.retries.retry_jitter", lambda: 0)
    native = OpenAIAdapter("fixture-key")
    transient = ModelFault("provider_rate_limited", "definite_failure", True, 0)
    failures = {
        "retry": [transient],
        "fallback": [transient, transient],
        "explicit": [transient, transient],
        "ambiguous": [ModelFault("provider_transport_interrupted", "ambiguous", True)],
        "nonretryable": [ModelFault("invalid_request", "definite_failure", False)],
    }[mode]
    adapter = Planned(native, failures)

    def factory(ctx, lease):
        execution = grounded.factory(ctx, lease)
        model = next(m for m in execution.catalog.registry if m.key == "paid_standard")
        execution.bindings = {
            **execution.bindings,
            "paid_standard": ModelBinding(model, adapter, context_packer(model, native)),
        }
        return execution

    try:
        run = grounded.admit(model_key="paid_standard" if mode == "explicit" else "auto")
        await grounded.execute(factory)
        row = grounded.row(run)
        if mode in ("retry", "fallback"):
            assert row[0] == "completed", row
        elif mode == "ambiguous":
            assert row[0] == "interrupted", row
        else:
            assert row[0] == "failed", row
        assert len(adapter.calls) == (2 if mode in ("retry", "fallback", "explicit") else 1)
        assert len(grounded.observed["calls"]) == (1 if mode == "fallback" else 0)
        with psycopg.connect(database["operator"]) as conn:
            attempts = conn.execute(
                "SELECT model_key,execution_state,state,failure_retryable FROM app.model_usage WHERE run_id=%s ORDER BY attempt_no",
                (run["id"],),
            ).fetchall()
            assert len(attempts) == (3 if mode == "fallback" else len(adapter.calls))
            assert all(a[2] != "reserved" for a in attempts)
        if mode == "fallback":
            old = row[2]["prior_manifests"][0]
            current = row[2]["prompt_manifest"]
            assert old["model_key"] == "paid_standard" and current["model_key"] == "local_standard"
            assert old["request_sha256"] != current["request_sha256"]
            assert old["sources"] == current["sources"]
            assert old["query_sha256"] == current["query_sha256"]
            assert grounded.observed["calls"][0]["options"]["num_ctx"] == 8192
    finally:
        await native.close()


@pytest.mark.parametrize("checkpoint", ["settlement", "manifest"])
async def test_recovery_after_definite_failure_uses_ledger_without_repeating_paid_transport(
    grounded, database, monkeypatch, checkpoint
):
    monkeypatch.setattr("evidencedesk.ai.retries.retry_jitter", lambda: 0)
    native = OpenAIAdapter("fixture-key")
    adapter = Planned(
        native, [ModelFault("provider_rate_limited", "definite_failure", True, 0)] * 2
    )
    from evidencedesk.ai import retries

    original = retries.next_attempt
    crashes = 0

    def crash_after_second(*args, **kwargs):
        nonlocal crashes
        if checkpoint == "settlement" and len(args[1]) == 2 and crashes == 0:
            crashes += 1
            raise RuntimeError("worker death after settled second failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(retries, "next_attempt", crash_after_second)

    from evidencedesk.db import answer_repository

    original_switch = answer_repository.switch_manifest

    async def switch_then_die(*args, **kwargs):
        nonlocal crashes
        await original_switch(*args, **kwargs)
        # Exiting the transaction commits the immutable fallback before simulating death.
        if checkpoint == "manifest" and crashes == 0:
            await args[0].commit()
            crashes += 1
            raise RuntimeError("worker death after fallback manifest")

    monkeypatch.setattr(answer_repository, "switch_manifest", switch_then_die)

    def factory(ctx, lease):
        execution = grounded.factory(ctx, lease)
        model = next(m for m in execution.catalog.registry if m.key == "paid_standard")
        execution.bindings = {
            **execution.bindings,
            "paid_standard": ModelBinding(model, adapter, context_packer(model, native)),
        }
        return execution

    try:
        run = grounded.admit(model_key="auto")
        with pytest.raises(RuntimeError, match="worker death"):
            await grounded.execute(factory)
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.jobs SET lease_until=now()-interval '1 second' WHERE run_id=%s",
                (run["id"],),
            )
        await grounded.jobs.sweep()
        with psycopg.connect(database["operator"]) as conn:
            conn.execute("UPDATE app.jobs SET available_at=now() WHERE run_id=%s", (run["id"],))
        await grounded.execute(factory)
        assert grounded.row(run)[0] == "completed", grounded.row(run)
        assert len(adapter.calls) == 2
        assert len(grounded.observed["calls"]) == 1
    finally:
        await native.close()


@pytest.mark.parametrize("selection", ["auto", "paid_standard"])
async def test_actual_packed_quote_excludes_hosted_route_when_budget_is_binding(
    grounded, database, selection
):
    native = OpenAIAdapter("fixture-key")
    adapter = Planned(native, [])

    def factory(ctx, lease):
        execution = grounded.factory(ctx, lease)
        model = next(m for m in execution.catalog.registry if m.key == "paid_standard")
        execution.bindings = {
            **execution.bindings,
            "paid_standard": ModelBinding(model, adapter, context_packer(model, native)),
        }
        return execution

    try:
        run = grounded.admit(model_key=selection)
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.workspaces SET daily_budget_micro_usd=1 WHERE id=%s",
                (grounded.base.split("/")[-1],),
            )
        await grounded.execute(factory)
        row = grounded.row(run)
        assert not adapter.calls
        if selection == "auto":
            assert row[0] == "completed", row
            assert row[2]["selected_model"]["key"] == "local_standard"
            assert row[2]["route_plan"]["keys"] == ["local_standard"]
            assert row[2]["route_plan"]["exclusions"][-1] == ["paid_standard", "budget_exceeded"]
            assert len(grounded.observed["calls"]) == 1
        else:
            assert row[:2] == ("failed", "budget_exceeded")
            assert not grounded.observed["calls"]
    finally:
        await native.close()


@pytest.mark.parametrize("constraint", ["local_only", "explicit_after_policy_change", "breaker"])
async def test_live_privacy_and_shared_breaker_control_actual_transport(
    grounded, database, monkeypatch, constraint
):
    from evidencedesk.models.health import ProviderHealth

    monkeypatch.setattr("evidencedesk.ai.retries.retry_jitter", lambda: 0)
    native = OpenAIAdapter("fixture-key")
    adapter = Planned(native, [ModelFault("provider_rate_limited", "definite_failure", True, 0)])

    def factory(ctx, lease):
        execution = grounded.factory(ctx, lease)
        model = next(m for m in execution.catalog.registry if m.key == "paid_standard")
        execution.bindings = {
            **execution.bindings,
            "paid_standard": ModelBinding(model, adapter, context_packer(model, native)),
        }
        execution.health = ProviderHealth(grounded.jobs.dispatch)
        return execution

    try:
        run = grounded.admit(
            model_key="paid_standard" if constraint == "explicit_after_policy_change" else "auto"
        )
        with psycopg.connect(database["operator"]) as conn:
            if constraint == "breaker":
                conn.execute(
                    "UPDATE app.provider_health SET failure_times=ARRAY[now(),now(),now(),now()],transient_failures=4 WHERE model_key='paid_standard'"
                )
            else:
                conn.execute(
                    "UPDATE app.workspaces SET privacy_mode='local_only',version=version+1 WHERE id=%s",
                    (grounded.base.split("/")[-1],),
                )
        await grounded.execute(factory)
        row = grounded.row(run)
        if constraint == "explicit_after_policy_change":
            assert row[0] == "failed", row
            assert not adapter.calls and not grounded.observed["calls"]
        else:
            assert row[0] == "completed", row
            assert len(adapter.calls) == (1 if constraint == "breaker" else 0)
            assert len(grounded.observed["calls"]) == 1
            assert row[2]["selected_model"]["key"] == "local_standard"
        if constraint == "breaker":
            with psycopg.connect(database["operator"]) as conn:
                assert conn.execute(
                    "SELECT state FROM app.provider_health WHERE model_key='paid_standard'"
                ).fetchone() == ("open",)
    finally:
        await native.close()

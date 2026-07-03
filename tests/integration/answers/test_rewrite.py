from uuid import uuid4

import psycopg
import pytest
from evidencedesk.ai.rewrite_experiment import RewriteExperiment
from evidencedesk.errors import Problem

from tests.integration.answers.test_execution import ANSWER
from tests.integration.answers.test_execution import grounded as grounded
from tests.integration.revisions.test_publication import scope

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("outcome", ["rewritten", "invalid", "unchanged", "disabled"])
async def test_scoped_rewrite_is_once_costed_and_does_not_replace_original_generation_query(
    grounded, database, outcome
):
    first = grounded.admit(query="How long are Lumen records retained?")
    await grounded.execute()
    followup = grounded.admit(
        conversation=first["conversation_id"], version=2, query="Can they be kept for 30 days?"
    )
    lease = await grounded.jobs.claim("chat", "rewrite-fixture")
    ctx = scope(lease)
    execution = grounded.factory(ctx, lease)
    binding = execution.bindings["local_standard"]
    experiment = RewriteExperiment(
        execution.db,
        binding,
        execution.catalog,
        execution.retriever.limits,
        enabled=outcome != "disabled",
    )
    grounded.observed["answer"] = {
        "query": "Can Lumen records be kept for 30 days?"
        if outcome == "rewritten"
        else "Can they be kept for 30 days?"
    }
    if outcome == "invalid":
        grounded.observed["raw"] = '{"query":"drop30","kb_ids":["foreign"]}'
    decision, retrieval = await experiment.retrieve(ctx, lease, execution.retriever)
    assert decision.original == "Can they be kept for 30 days?"
    assert decision.changed == (outcome == "rewritten")
    calls = len(grounded.observed["calls"])
    assert calls == (1 if outcome == "disabled" else 2)
    assert await experiment.run(ctx, lease) == decision
    assert len(grounded.observed["calls"]) == calls
    if outcome != "disabled":
        body = grounded.observed["calls"][-1]
        assert body["options"]["num_predict"] == 256
        assert body["format"]["properties"].keys() == {"query"}
        assert "How long are Lumen records retained?" in body["messages"][-1]["content"]
        assert "Fixture evidence passage" not in body["messages"][-1]["content"]
    grounded.observed["answer"] = ANSWER
    grounded.observed["raw"] = None

    def with_experimental_retrieval(ctx, claimed):
        worker = grounded.factory(ctx, claimed)
        worker.retrieved = retrieval
        return worker

    await grounded.runner.execute(lease, uuid4(), with_experimental_retrieval)
    assert grounded.row(followup)[0] == "completed", grounded.row(followup)
    with psycopg.connect(database["operator"]) as conn:
        rows = conn.execute(
            "SELECT purpose,state FROM app.model_usage WHERE run_id=%s ORDER BY attempt_no",
            (followup["id"],),
        ).fetchall()
        assert rows == ([("rewrite", "settled")] if outcome != "disabled" else []) + [
            ("generation", "settled")
        ]
    assert (
        "Can they be kept for 30 days?" in grounded.observed["calls"][-1]["messages"][-1]["content"]
    )


@pytest.mark.parametrize("change", ["privacy", "history", "membership"])
async def test_rewrite_reauthorizes_after_provider_and_excludes_revoked_history(
    grounded, database, change
):
    first = grounded.admit(query="How long are Lumen records retained?")
    await grounded.execute()
    followup = grounded.admit(
        conversation=first["conversation_id"], version=2, query="What about their archive?"
    )
    lease = await grounded.jobs.claim("chat", "rewrite-revocation")
    ctx = scope(lease)
    execution = grounded.factory(ctx, lease)
    experiment = RewriteExperiment(
        execution.db,
        execution.bindings["local_standard"],
        execution.catalog,
        execution.retriever.limits,
        enabled=True,
    )
    grounded.observed["answer"] = {"query": "What about the Lumen record archive?"}

    def revoke():
        with psycopg.connect(database["operator"]) as conn:
            if change == "privacy":
                conn.execute(
                    "UPDATE app.workspaces SET privacy_mode='local_only',version=version+1 WHERE id=%s",
                    (ctx.workspace_id,),
                )
            elif change == "history":
                conn.execute(
                    "UPDATE app.knowledge_bases SET deleted_at=now() WHERE id=%s", (grounded.kb,)
                )
            else:
                conn.execute(
                    "DELETE FROM app.memberships WHERE workspace_id=%s AND user_id=%s",
                    (ctx.workspace_id, ctx.user_id),
                )

    grounded.observed["hook"] = revoke
    with pytest.raises(Problem):
        await experiment.run(ctx, lease)
    with psycopg.connect(database["operator"]) as conn:
        assert conn.execute(
            "SELECT purpose,state FROM app.model_usage WHERE run_id=%s", (followup["id"],)
        ).fetchall() == [("rewrite", "estimated")]


@pytest.mark.parametrize("restriction", ["local_only", "explicit_local"])
async def test_hosted_rewriter_cannot_override_private_or_explicit_model(
    grounded, database, restriction
):
    import httpx2
    from evidencedesk.ai.orchestrator import ModelBinding
    from evidencedesk.ai.token_budget import context_packer
    from evidencedesk.providers.openai import OpenAIAdapter

    first = grounded.admit(query="How long are Lumen records retained?")
    await grounded.execute()
    if restriction == "local_only":
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.workspaces SET privacy_mode='local_only',version=version+1 WHERE id=%s",
                (grounded.base.split("/")[-1],),
            )
    followup = grounded.admit(
        conversation=first["conversation_id"],
        version=2,
        query="What about their archive?",
        model_key="auto" if restriction == "local_only" else "local_standard",
    )
    lease = await grounded.jobs.claim("chat", "rewrite-private")
    ctx = scope(lease)
    execution = grounded.factory(ctx, lease)
    model = next(m for m in execution.catalog.registry if m.key == "paid_standard")

    def forbidden(request):
        raise AssertionError("Private rewrite reached hosted transport")

    native = OpenAIAdapter("fixture-secret", transport=httpx2.MockTransport(forbidden))
    try:
        binding = ModelBinding(model, native, context_packer(model, native))
        experiment = RewriteExperiment(
            execution.db, binding, execution.catalog, execution.retriever.limits, enabled=True
        )
        result = await experiment.run(ctx, lease)
        assert result.reason == "rewrite_model_unavailable" and not result.changed
        with psycopg.connect(database["operator"]) as conn:
            assert conn.execute(
                "SELECT count(*) FROM app.model_usage WHERE run_id=%s", (followup["id"],)
            ).fetchone() == (0,)
    finally:
        await native.close()


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_failed_rewrite_never_retries_and_preserves_conservative_accounting(
    grounded, database, ambiguous
):
    from evidencedesk.ai.orchestrator import ModelBinding
    from evidencedesk.providers.base import ModelFault

    first = grounded.admit(query="How long are Lumen records retained?")
    await grounded.execute()
    followup = grounded.admit(
        conversation=first["conversation_id"], version=2, query="What about their archive?"
    )
    lease = await grounded.jobs.claim("chat", "rewrite-fault")
    ctx = scope(lease)
    execution = grounded.factory(ctx, lease)
    native = execution.bindings["local_standard"]
    calls = []

    class Fault:
        def estimate(self, request):
            return native.adapter.estimate(request)

        async def stream(self, request):
            calls.append(request)
            yield ModelFault(
                "provider_transport_interrupted" if ambiguous else "provider_rate_limited",
                "ambiguous" if ambiguous else "definite_failure",
                True,
            )

    binding = ModelBinding(native.model, Fault(), native.packer)
    experiment = RewriteExperiment(
        execution.db, binding, execution.catalog, execution.retriever.limits, enabled=True
    )
    for _ in range(2):
        if ambiguous:
            with pytest.raises(Problem, match="provider_interrupted"):
                await experiment.run(ctx, lease)
        else:
            decision = await experiment.run(ctx, lease)
            assert decision.reason == "rewrite_unavailable" and not decision.changed
    assert len(calls) == 1
    await grounded.runner.execute(lease, uuid4(), grounded.factory)
    assert grounded.row(followup)[0] == ("interrupted" if ambiguous else "completed")
    with psycopg.connect(database["operator"]) as conn:
        rows = conn.execute(
            "SELECT purpose,state FROM app.model_usage WHERE run_id=%s ORDER BY attempt_no",
            (followup["id"],),
        ).fetchall()
        assert rows == (
            [("rewrite", "estimated")]
            if ambiguous
            else [("rewrite", "settled"), ("generation", "settled")]
        )

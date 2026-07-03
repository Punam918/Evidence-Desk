import json
from uuid import uuid4

import psycopg
import pytest
from evidencedesk.ai.summary_experiment import SummaryExperiment
from evidencedesk.db import summary_repository
from evidencedesk.db.session import authorized_transaction
from evidencedesk.errors import Problem

from tests.integration.answers.test_execution import ANSWER
from tests.integration.answers.test_execution import grounded as grounded
from tests.integration.revisions.test_publication import scope

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("outcome", ["valid", "foreign_provenance", "stale", "disabled"])
async def test_optional_summary_is_bounded_once_costed_and_optimistically_stored(
    grounded, database, outcome
):
    conversation = None
    for version in range(1, 8):
        prior = grounded.admit(
            conversation=conversation, version=version, query=f"Lumen retention question {version}"
        )
        await grounded.execute()
        conversation = prior["conversation_id"]
    run = grounded.admit(
        conversation=conversation, version=8, query="What does the archive retain?"
    )
    lease = await grounded.jobs.claim("chat", "summary-fixture")
    ctx = scope(lease)
    execution = grounded.factory(ctx, lease)
    experiment = SummaryExperiment(
        execution.db,
        execution.bindings["local_standard"],
        execution.catalog,
        enabled=outcome != "disabled",
    )
    issued = []

    def output(body):
        assert body["options"]["num_predict"] == 384
        context = (
            body["messages"][-1]["content"].split("<evidence>\n", 1)[1].split("\n</evidence>", 1)[0]
        )
        messages = json.loads(context)["untrusted_older_messages"]
        assert len(messages) == 2 and "question 1" in messages[0]["text"]
        assert all("question 7" not in msg["text"] for msg in messages)
        issued.append(body)
        return {
            "facts": [
                {
                    "text": "Lumen retention was discussed.",
                    "message_ids": [
                        str(uuid4())
                        if outcome == "foreign_provenance"
                        else messages[0]["message_id"]
                    ],
                }
            ],
            "open_questions": [],
        }

    grounded.observed["answer_factory"] = output
    if outcome == "stale":

        def change_version():
            with psycopg.connect(database["operator"]) as conn:
                conn.execute(
                    "UPDATE app.conversations SET version=version+1 WHERE id=%s", (conversation,)
                )

        grounded.observed["hook"] = change_version
    result = await experiment.run(ctx, lease)
    if outcome == "valid":
        assert result and 0 < result.tokens <= 384
        assert result.through_sequence == 2
        assert await experiment.run(ctx, lease) == result
        assert len(issued) == 1
        async with authorized_transaction(execution.db, ctx) as conn:
            assert await summary_repository.saved(conn, ctx, lease) == result
    else:
        assert result is None
    with psycopg.connect(database["operator"]) as conn:
        summary = conn.execute(
            "SELECT summary FROM app.conversations WHERE id=%s", (conversation,)
        ).fetchone()[0]
        assert bool(summary) == (outcome == "valid")
        rows = conn.execute(
            "SELECT purpose,state FROM app.model_usage WHERE run_id=%s", (run["id"],)
        ).fetchall()
        assert rows == (
            []
            if outcome == "disabled"
            else [("summary", "estimated" if outcome == "stale" else "settled")]
        )
    if outcome == "valid":
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.knowledge_bases SET deleted_at=now() WHERE id=%s", (grounded.kb,)
            )
        async with authorized_transaction(execution.db, ctx) as conn:
            with pytest.raises(Problem, match="source_revoked"):
                await summary_repository.saved(conn, ctx, lease)
        assert await experiment.run(ctx, lease) is None
    elif outcome in ("foreign_provenance", "disabled"):
        grounded.observed["answer_factory"] = None
        grounded.observed["answer"] = ANSWER
        await grounded.runner.execute(lease, uuid4(), grounded.factory)
        assert grounded.row(run)[0] == "completed", grounded.row(run)

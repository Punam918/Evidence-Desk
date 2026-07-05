from dataclasses import replace
from uuid import UUID, uuid4

import psycopg
import pytest
from evidencedesk.ai.context import ContextBuilder
from evidencedesk.db.history_repository import load_history
from evidencedesk.db.session import authorized_transaction
from evidencedesk.errors import Problem
from evidencedesk.retrieval.rerank import Reranker
from psycopg.types.json import Jsonb

from tests.integration.retrieval.test_reranking import ScriptedScorer
from tests.integration.retrieval.test_retrieval import publish_fixture
from tests.integration.retrieval.test_retrieval import retriever as retriever
from tests.integration.revisions.test_publication import scope
from tests.security.uploads.test_uploads import setup
from tests.support import sign_in
from tests.unit.ai.test_context import packer

pytestmark = pytest.mark.integration


def seed_history(database, ctx, conversation):
    runs = []
    with psycopg.connect(database["operator"]) as conn:
        for i in range(9):
            run, user, assistant = uuid4(), uuid4(), uuid4()
            state = "queued" if i == 8 else "failed" if i == 5 else "completed"
            conn.execute(
                """INSERT INTO app.messages(workspace_id,id,conversation_id,user_id,sequence,role,state,text)
                VALUES(%s,%s,%s,%s,%s,'user','final',%s),(%s,%s,%s,%s,%s,'assistant',%s,%s)""",
                (
                    ctx.workspace_id,
                    user,
                    conversation,
                    ctx.user_id,
                    i * 2 + 1,
                    "Fixture" if i == 8 else f"User {i}",
                    ctx.workspace_id,
                    assistant,
                    conversation,
                    ctx.user_id,
                    i * 2 + 2,
                    "draft" if i == 8 else "failed" if i == 5 else "final",
                    f"Assistant {i}",
                ),
            )
            conn.execute(
                """INSERT INTO app.runs(workspace_id,id,conversation_id,user_id,user_message_id,
                assistant_message_id,status,intent,requested_model_key,config_sha256,prompt_version)
                VALUES(%s,%s,%s,%s,%s,%s,%s,'ask','local_standard','fixture','v1')""",
                (
                    ctx.workspace_id,
                    run,
                    conversation,
                    ctx.user_id,
                    user,
                    assistant,
                    state,
                ),
            )
            runs.append(run)
        conn.execute(
            "UPDATE app.runs SET evidence=%s WHERE id=%s",
            (
                Jsonb(
                    [
                        {
                            "evidence_id": "E1",
                            "chunk_id": str(uuid4()),
                            "document_id": str(uuid4()),
                            "revision_id": str(uuid4()),
                        }
                    ]
                ),
                runs[6],
            ),
        )
    return runs


async def test_private_completed_history_and_context_live_source_recheck(
    auth_client,
    object_store,
    database,
    services,
    retriever,
    tmp_path,
    monkeypatch,
):
    client, _, _, h, base, url = setup(auth_client)
    lease, artifact = await publish_fixture(
        client, url, h, database, object_store, services, tmp_path
    )
    ctx = scope(lease)
    conversation = UUID(
        client.post(
            base + "/conversations",
            json={"title": "Context"},
            headers={**h, "Idempotency-Key": str(uuid4())},
        ).json()["id"]
    )
    runs = seed_history(database, ctx, conversation)
    async with authorized_transaction(retriever.db, ctx) as conn:
        history = await load_history(conn, ctx, runs[-1])
    # Only six most recent completed pairs are considered: 1,2,3,4,6,7. Revoked6 is excluded.
    assert [p.user for p in history] == ["User 1", "User 2", "User 3", "User 4", "User 7"]
    result = await retriever.retrieve(ctx, "Fixture", [artifact.kb_id], "hybrid_rerank")
    ranked = await Reranker(retriever.db, ScriptedScorer()).rank(ctx, "Fixture", result)
    builder = ContextBuilder(retriever.db, packer())
    bundle = await builder.build(ctx, runs[-1], "Fixture", ranked)
    assert bundle.request and len(bundle.history_ids) == 10
    assert all(e.passage.published_at is not None for e in bundle.evidence)
    original = builder.packer.pack

    def revoke_during_pack(*args):
        with psycopg.connect(database["operator"]) as conn:
            conn.execute(
                "UPDATE app.runs SET evidence=%s WHERE id=%s",
                (
                    Jsonb(
                        [
                            {
                                "evidence_id": "E1",
                                "chunk_id": str(uuid4()),
                                "document_id": str(uuid4()),
                                "revision_id": str(uuid4()),
                            }
                        ]
                    ),
                    runs[7],
                ),
            )
        return original(*args)

    monkeypatch.setattr(builder.packer, "pack", revoke_during_pack)
    with pytest.raises(Problem, match="source_revoked"):
        await builder.build(ctx, runs[-1], "Fixture", ranked)
    # A different workspace owner cannot hydrate this creator's history.
    state = auth_client[1]
    state["subject"] = str(uuid4())
    other = sign_in(client, state)
    other_id = UUID(other["user"]["id"])
    with psycopg.connect(database["operator"]) as conn:
        conn.execute(
            "INSERT INTO app.memberships(workspace_id,user_id,role) VALUES(%s,%s,'owner')",
            (ctx.workspace_id, other_id),
        )
    async with authorized_transaction(
        retriever.db, replace(ctx, user_id=other_id, role="owner")
    ) as conn:
        with pytest.raises(Problem, match="not_found"):
            await load_history(conn, replace(ctx, user_id=other_id, role="owner"), runs[-1])
    with psycopg.connect(database["operator"]) as conn:
        conn.execute("UPDATE app.conversations SET deleted_at=now() WHERE id=%s", (conversation,))
    async with authorized_transaction(retriever.db, ctx) as conn:
        with pytest.raises(Problem, match="not_found"):
            await load_history(conn, ctx, runs[-1])

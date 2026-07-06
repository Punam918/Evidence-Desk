from uuid import uuid4

import psycopg
import pytest
from evidencedesk.errors import Problem
from evidencedesk.workflows.answer import build_answer_graph
from evidencedesk.workflows.checkpoint_codec import CheckpointCodec
from evidencedesk.workflows.state import ReferenceState, serialize
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row

pytestmark = pytest.mark.integration


class ScriptedSteps:
    def __init__(self, evidence=True, fail=None):
        self.evidence = evidence
        self.fail = fail
        self.calls = []
        self.failures = []

    async def perform(self, node, state):
        self.calls.append(node)
        if node == self.fail:
            raise Problem(503, "fixture_failure", "A scripted dependency failed.")
        changes = {"step": node}
        if node == "retrieve" and self.evidence:
            changes["evidence_chunk_ids"] = [uuid4()]
        if node in ("finalize", "terminalize"):
            changes["step"] = "done"
        return state.model_copy(update=changes)

    async def record_failure(self, state, problem):
        self.failures.append(problem.code)


@pytest.mark.parametrize("evidence", [True, False])
async def test_real_saver_paths_and_completed_replay(database, evidence):
    thread = uuid4()
    state = ReferenceState(
        thread_id=thread,
        run_id=uuid4(),
        intent="ask",
        revision_ids=[],
        evidence_chunk_ids=[],
        step="authorize",
        config_sha256="a" * 64,
    )
    config = {"configurable": {"thread_id": str(thread)}, "recursion_limit": 16}
    steps = ScriptedSteps(evidence=evidence)
    async with await psycopg.AsyncConnection.connect(
        database["ed_checkpoint"], autocommit=True, row_factory=dict_row
    ) as conn:
        graph = build_answer_graph(steps, AsyncPostgresSaver(conn, serde=CheckpointCodec()))
        result = await graph.ainvoke(serialize(state), config, durability="sync")
        assert result["step"] == "done"
        expected = (
            ["authorize", "route", "retrieve"]
            + (["context", "generate", "validate"] if evidence else ["abstain"])
            + ["finalize"]
        )
        assert steps.calls == expected
    # A newly created graph/saver resumes the persisted terminal checkpoint without rerunning nodes.
    resumed = ScriptedSteps()
    async with await psycopg.AsyncConnection.connect(
        database["ed_checkpoint"], autocommit=True, row_factory=dict_row
    ) as conn:
        graph = build_answer_graph(resumed, AsyncPostgresSaver(conn, serde=CheckpointCodec()))
        assert (await graph.ainvoke(None, config, durability="sync"))["step"] == "done"
        assert resumed.calls == []
        assert set((await graph.aget_state(config)).values) == set(serialize(state))


@pytest.mark.parametrize("failing_node", ["retrieve", "generate", "finalize"])
async def test_typed_failure_reaches_terminalize(database, failing_node):
    state = ReferenceState(
        thread_id=uuid4(),
        run_id=uuid4(),
        intent="ask",
        revision_ids=[],
        evidence_chunk_ids=[],
        step="authorize",
        config_sha256="b" * 64,
    )
    steps = ScriptedSteps(fail=failing_node)
    async with await psycopg.AsyncConnection.connect(
        database["ed_checkpoint"], autocommit=True, row_factory=dict_row
    ) as conn:
        graph = build_answer_graph(steps, AsyncPostgresSaver(conn, serde=CheckpointCodec()))
        result = await graph.ainvoke(
            serialize(state),
            {"configurable": {"thread_id": str(state.thread_id)}, "recursion_limit": 16},
            durability="sync",
        )
        assert result["step"] == "done"
        assert steps.calls[-1] == "terminalize"
        assert steps.failures == ["fixture_failure"]

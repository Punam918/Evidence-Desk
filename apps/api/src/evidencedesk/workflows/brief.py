"""Separate approval graph. Resume values are hints; only the business ledger authorizes a save."""

from typing import Any, Literal, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from evidencedesk.observability.stages import event, stage
from evidencedesk.workflows.state import GraphState, ReferenceState, serialize

BriefNode = Literal["prepare", "approval", "reauthorize", "save"]


class BriefSteps(Protocol):
    async def perform(self, node: BriefNode, state: ReferenceState) -> ReferenceState: ...


def build_brief_graph(
    steps: BriefSteps, saver: BaseCheckpointSaver[Any]
) -> CompiledStateGraph[Any, Any, Any, Any]:
    graph = StateGraph(GraphState)

    def node(name: BriefNode):  # type: ignore[no-untyped-def]
        async def invoke(raw: GraphState) -> GraphState:
            state = ReferenceState.model_validate(raw)
            with stage("brief." + name):
                state = await steps.perform(name, state)
            if name == "approval" and state.step != "done":
                # Ignore returned decision. The following node reloads approved_by/hash/expiry.
                event("graph.interrupt")
                interrupt(
                    {
                        "execution_id": str(state.tool_execution_id),
                        "payload_sha256": state.config_sha256,
                    }
                )
            return serialize(state)

        return invoke

    for name in ("prepare", "approval", "reauthorize", "save"):
        graph.add_node(name, node(name))
    graph.add_edge(START, "prepare")
    for before, after in (
        ("prepare", "approval"),
        ("approval", "reauthorize"),
        ("reauthorize", "save"),
        ("save", END),
    ):

        def select(state: GraphState, destination: str = after) -> str:
            return END if state["step"] == "done" else destination

        graph.add_conditional_edges(before, select, {END: END, after: after})
    return graph.compile(checkpointer=saver)

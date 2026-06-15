"""Bounded answer graph. Nodes delegate effects to live-authorized application services."""

from collections.abc import Awaitable, Callable, Hashable
from typing import Any, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from evidencedesk.errors import LeaseLost, Problem
from evidencedesk.observability.stages import stage
from evidencedesk.workflows.state import AnswerNode, GraphState, ReferenceState, serialize


class AsyncNode(Protocol):
    def __call__(self, state: GraphState) -> Awaitable[GraphState]: ...


class AnswerSteps(Protocol):
    async def perform(self, node: AnswerNode, state: ReferenceState) -> ReferenceState: ...
    async def record_failure(self, state: ReferenceState, problem: Problem) -> None: ...


def build_answer_graph(
    steps: AnswerSteps, saver: BaseCheckpointSaver[Any]
) -> CompiledStateGraph[Any, Any, Any, Any]:
    graph = StateGraph(GraphState)

    def node(name: AnswerNode) -> AsyncNode:
        async def invoke(state: GraphState) -> GraphState:
            parsed = ReferenceState.model_validate(state)
            try:
                with stage("answer." + name):
                    updated = await steps.perform(name, parsed)
                # Revalidate even internal mutations; never permit content/authority in checkpoints.
                return serialize(ReferenceState.model_validate(updated.model_dump()))
            except LeaseLost:
                # A stale worker must not append a terminal event or make another effect.
                raise
            except Problem as exc:
                if name == "terminalize":
                    raise
                await steps.record_failure(parsed, exc)
                return serialize(parsed.model_copy(update={"step": "terminalize"}))

        return invoke

    names: list[AnswerNode] = [
        "authorize",
        "route",
        "retrieve",
        "context",
        "generate",
        "validate",
        "finalize",
        "abstain",
        "terminalize",
    ]
    for name in names:
        graph.add_node(name, node(name))
    graph.add_edge(START, "authorize")

    def select(default: str) -> Callable[[GraphState], str]:
        def next_node(state: GraphState) -> str:
            if state["step"] == "done":
                return END
            if state["step"] == "terminalize":
                return "terminalize"
            if default == "context" and not state["evidence_chunk_ids"]:
                return "abstain"
            if default == "generate" and state["step"] == "abstain":
                return "abstain"
            return default

        return next_node

    for source, target in [
        ("authorize", "route"),
        ("route", "retrieve"),
        ("retrieve", "context"),
        ("context", "generate"),
        ("generate", "validate"),
        ("validate", "finalize"),
        ("abstain", "finalize"),
    ]:
        destinations: dict[Hashable, str] = {target: target, "terminalize": "terminalize", END: END}
        if source in ("retrieve", "context"):
            destinations["abstain"] = "abstain"
        graph.add_conditional_edges(source, select(target), destinations)
    graph.add_conditional_edges("finalize", select(END), {END: END, "terminalize": "terminalize"})
    graph.add_edge("terminalize", END)
    return graph.compile(checkpointer=saver)

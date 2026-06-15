"""Public saver overrides time persistence without inspecting checkpoint content."""

from collections.abc import Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from evidencedesk.observability.stages import instrument


class ObservedSaver(AsyncPostgresSaver):
    @instrument("checkpoint.delete")
    async def adelete_thread(self, thread_id: str) -> None:
        await super().adelete_thread(thread_id)

    @instrument("checkpoint.read")
    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await super().aget_tuple(config)

    @instrument("checkpoint.write")
    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await super().aput(config, checkpoint, metadata, new_versions)

    @instrument("checkpoint.pending_write")
    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await super().aput_writes(config, writes, task_id, task_path)

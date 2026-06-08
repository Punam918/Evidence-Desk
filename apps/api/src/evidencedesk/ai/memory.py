"""Ephemeral completed conversation pair; never stored in graph checkpoints."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class HistoryPair:
    user_message_id: UUID
    assistant_message_id: UUID
    user: str
    assistant: str

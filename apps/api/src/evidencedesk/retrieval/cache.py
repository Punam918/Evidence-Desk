"""Process-local vector cache; authorization and source hydration are never cached."""

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from evidencedesk.embeddings.models import SparseVector

Key = tuple[UUID, str, str, str]
Vectors = tuple[list[float] | None, SparseVector | None]


@dataclass(frozen=True)
class Entry:
    expires: float
    dense: tuple[float, ...] | None
    indices: tuple[int, ...] | None
    values: tuple[float, ...] | None


class QueryEmbeddingCache:
    """Owned by one event loop. Entries contain hashes and vectors, never query strings."""

    def __init__(
        self,
        capacity: int = 1024,
        ttl: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not 1 <= capacity <= 1024 or not 0 < ttl <= 300:
            raise ValueError("Query cache exceeds the approved bounds")
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self.entries: OrderedDict[Key, Entry] = OrderedDict()

    @staticmethod
    def key(workspace: UUID, encoder_hash: str, profile: str, normalized_query: str) -> Key:
        return (
            workspace,
            encoder_hash,
            profile,
            hashlib.sha256(("nfc_strip_v1:" + normalized_query).encode()).hexdigest(),
        )

    def get(self, key: Key) -> Vectors | None:
        entry = self.entries.get(key)
        if entry is None:
            return None
        if entry.expires <= self.clock():
            del self.entries[key]
            return None
        self.entries.move_to_end(key)
        return (
            list(entry.dense) if entry.dense is not None else None,
            SparseVector(indices=list(entry.indices), values=list(entry.values))
            if entry.indices is not None and entry.values is not None
            else None,
        )

    def put(self, key: Key, dense: list[float] | None, sparse: SparseVector | None) -> None:
        now = self.clock()
        for old in [k for k, entry in self.entries.items() if entry.expires <= now]:
            del self.entries[old]
        self.entries[key] = Entry(
            now + self.ttl,
            tuple(dense) if dense is not None else None,
            tuple(sparse.indices) if sparse is not None else None,
            tuple(sparse.values) if sparse is not None else None,
        )
        self.entries.move_to_end(key)
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)

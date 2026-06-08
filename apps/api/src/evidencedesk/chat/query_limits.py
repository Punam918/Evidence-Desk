"""Query token preflight uses the exact pinned embedding tokenizer, without truncation."""

import asyncio
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from tokenizers import Tokenizer

from evidencedesk.errors import Problem
from evidencedesk.models.tokenizer import generation_tokenizer


@lru_cache(maxsize=4)
def tokenizer(cache: str, lock_path: str) -> Tokenizer:
    lock = json.loads(Path(lock_path).read_text())
    profile = next(m for m in lock["models"] if m["kind"] == "embedding")
    path = (
        Path(cache)
        / ("models--" + profile["model_id"].replace("/", "--"))
        / "snapshots"
        / profile["revision"]
        / "tokenizer.json"
    )
    if hashlib.sha256(path.read_bytes()).hexdigest() != profile["files_sha256"]["tokenizer.json"]:
        raise ValueError("Tokenizer artifact failed integrity check")
    result = Tokenizer.from_file(str(path))
    result.no_truncation()
    return result


class QueryLimits:
    def __init__(self, cache: Path, lock: Path):
        self.cache, self.lock = cache, lock

    def check_sync(self, query: str) -> None:
        try:
            count = len(
                tokenizer(str(self.cache), str(self.lock))
                .encode(query, add_special_tokens=True)
                .ids
            )
            if count > 256:
                raise Problem(422, "query_too_long", "Query exceeds the 256 embedding-token limit.")
            # Baseline preflight. Each chosen adapter must also enforce its own exact tokenizer cap.
            if len(generation_tokenizer(self.cache).encode(query, disallowed_special=())) > 512:
                raise Problem(
                    422, "query_too_long", "Query exceeds the 512 generation-token limit."
                )
        except (OSError, ValueError, KeyError, StopIteration) as exc:
            raise Problem(
                503, "tokenizer_unavailable", "Query validation is temporarily unavailable.", True
            ) from exc

    async def check(self, query: str) -> None:
        await asyncio.to_thread(self.check_sync, query)

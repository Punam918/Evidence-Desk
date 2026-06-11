"""Offline-only generation tokenizer construction from a checksum-pinned local artifact."""

import base64
import hashlib
import json
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path

import tiktoken


@lru_cache(maxsize=4)
def generation_tokenizer(
    cache: Path = Path(".cache/models"),
    lock: Path = Path("contracts/config/generation-tokenizer.lock.json"),
) -> tiktoken.Encoding:
    config = json.loads(lock.read_text())
    if config["library"] != "tiktoken==" + version("tiktoken") or config["name"] != "o200k_base":
        raise ValueError("Generation tokenizer library or encoding differs from its lock")
    data = (cache / "tiktoken" / "o200k_base.tiktoken").read_bytes()
    if hashlib.sha256(data).hexdigest() != config["sha256"]:
        raise ValueError("Generation tokenizer artifact failed integrity checking")
    ranks = {
        base64.b64decode(token, validate=True): int(rank)
        for token, rank in (line.split() for line in data.splitlines())
    }
    # Construct directly; tiktoken.get_encoding can download on a cache miss and is not used here.
    return tiktoken.Encoding(
        name=config["name"],
        pat_str=config["pattern"],
        mergeable_ranks=ranks,
        special_tokens=config["special_tokens"],
    )

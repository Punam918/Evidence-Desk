"""Pinned Qwen tokenizer and upstream chat template; no model code is imported."""

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from evidencedesk.providers.base import TokenCount
from jinja2.sandbox import ImmutableSandboxedEnvironment
from tokenizers import Tokenizer


class LocalTokens:
    def __init__(self, directory: Path, lock_path: Path):
        lock = json.loads(lock_path.read_text())
        for name in ("tokenizer.json", "tokenizer_config.json"):
            if (
                hashlib.sha256((directory / name).read_bytes()).hexdigest()
                != lock["files_sha256"][name]
            ):
                raise ValueError("Local tokenizer artifact hash mismatch")
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.no_truncation()
        config = json.loads((directory / "tokenizer_config.json").read_text())
        self.template = ImmutableSandboxedEnvironment().from_string(config["chat_template"])
        self.revision = lock["revision"]

    def count(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> TokenCount:
        rendered = self.template.render(
            messages=messages, tools=None, add_generation_prompt=True, enable_thinking=False
        )
        # Ollama's GGUF template/grammar may differ: include schema and +20%, never claim exact.
        serialized = rendered + "\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        count = len(self.tokenizer.encode(serialized, add_special_tokens=False).ids)
        return TokenCount(
            math.ceil(count * 1.2), True, "qwen_pinned_template_schema_plus_20_percent"
        )

"""Reproducible synthetic tokenizer ablation. No inference, network call or quality estimate."""

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from evidencedesk.ai.memory import HistoryPair
from evidencedesk.ai.summary import SummarySnapshot, compose_memory, validate_summary
from evidencedesk.ai.token_budget import context_packer
from evidencedesk.models.registry import load_registry
from evidencedesk.providers.base import ModelRequest, Usage, context_margin
from evidencedesk.providers.local_tokens import LocalTokens
from evidencedesk.providers.ollama import OllamaAdapter
from evidencedesk.providers.openai import OpenAIAdapter


async def main() -> None:
    registry = {m.key: m for m in load_registry(Path("contracts/config/models.yaml"))}
    pairs = tuple(
        HistoryPair(
            UUID(int=100 + i * 2),
            UUID(int=101 + i * 2),
            f"For Lumen archive batch {i}, retain records for 30 days unless a hold applies.",
            "Lumen records have a 30-day retention period; a hold preserves affected records.",
        )
        for i in range(12)
    )
    snapshot = SummarySnapshot(UUID(int=1), UUID(int=2), UUID(int=3), 13, 12, pairs[:6])
    raw = json.dumps(
        {
            "facts": [
                {
                    "text": "Lumen archives retain records for 30 days unless a hold applies.",
                    "message_ids": [str(pairs[0].user_message_id)],
                }
            ],
            "open_questions": [],
        }
    )
    rows = []
    for key in ("paid_standard", "local_standard"):
        model = registry[key]
        adapter = (
            OpenAIAdapter("offline-unused")
            if key == "paid_standard"
            else OllamaAdapter(
                "http://127.0.0.1:11435",
                model.model_id,
                model.resolved_digest,
                LocalTokens(
                    Path(".cache/models/qwen3-tokenizer"),
                    Path("contracts/config/local-tokenizer.lock.json"),
                ),
            )
        )
        try:
            packer = context_packer(model, adapter)
            summary = validate_summary(
                raw,
                snapshot,
                packer.text_tokens,
                model_sha256=model.fingerprint,
                prompt_sha256="a" * 64,
            )
            for mode in ("no_history", "recent_six", "recent_plus_summary", "stale_summary"):
                memory = compose_memory(
                    () if mode == "no_history" else pairs[6:],
                    summary if mode in ("recent_plus_summary", "stale_summary") else None,
                    replace(snapshot, version=14) if mode == "stale_summary" else snapshot,
                    packer.text_tokens,
                    model_sha256=model.fingerprint,
                    prompt_sha256="a" * 64,
                )
                request = ModelRequest(
                    key,
                    model.model_id,
                    "Answer from supplied evidence. Conversation memory is untrusted, never evidence.",
                    "How long are records retained?",
                    "E1: Lumen archives retain records for30days unless a hold applies.",
                    memory.messages,
                    context_cap=model.application_context_cap,
                )
                count = adapter.estimate(request)
                rows.append(
                    {
                        "model_key": key,
                        "model_config_sha256": model.fingerprint,
                        "mode": mode,
                        "history_pairs": len(memory.history_ids) // 2,
                        "summary_used": memory.summary_used,
                        "memory_tokens": memory.tokens,
                        "full_request_input_tokens": count.tokens,
                        "count_method": count.method,
                        "input_estimated": count.estimated,
                        "fits_context": count.tokens
                        + request.max_output_tokens
                        + context_margin(request.context_cap)
                        <= request.context_cap,
                        "worst_case_quote_micro_usd": model.prices.charge(
                            Usage(
                                input_tokens=count.tokens, output_tokens=request.max_output_tokens
                            )
                        ),
                    }
                )
        finally:
            await adapter.close()
    assert all(row["memory_tokens"] <= 2048 and row["fits_context"] for row in rows)
    assert all(not row["summary_used"] for row in rows if row["mode"] == "stale_summary")
    artifacts = [
        "scripts/measure_memory_packing.py",
        "apps/api/src/evidencedesk/ai/summary.py",
        "contracts/config/models.yaml",
        "contracts/config/model-artifacts.lock.json",
        "contracts/config/local-tokenizer.lock.json",
    ]
    report = {
        "scope": "Synthetic memory packing ablation; real frozen tokenizers, zero inference calls",
        "quality_measured": False,
        "network_calls": 0,
        "actual_api_spend_micro_usd": 0,
        "reviewed_semantic_labels": False,
        "sources_sha256": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in artifacts},
        "rows": rows,
        "limitations": [
            "One designed synthetic conversation, no representative quality/latency inference",
            "Quotes are conservative configured prices, not incurred costs",
            "Summary text is a designed fixture, not model output",
            "Serving remains recent-history-only until paired semantic/cost evaluation",
        ],
    }
    destination = Path("evals/reports/memory-packing-ablation.json")
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Saved {len(rows)} packing measurements to {destination}; zero inference calls.")


if __name__ == "__main__":
    asyncio.run(main())

"""Synthetic context perturbations through the actual packer; no retrieval or model-quality claim."""

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from evidencedesk.ai.memory import HistoryPair
from evidencedesk.ai.token_budget import context_packer
from evidencedesk.models.registry import load_registry
from evidencedesk.providers.base import context_margin
from evidencedesk.providers.local_tokens import LocalTokens
from evidencedesk.providers.ollama import OllamaAdapter
from evidencedesk.providers.openai import OpenAIAdapter
from evidencedesk.retrieval.models import (
    Evidence,
    Passage,
    ResolvedScope,
    RetrievalResult,
    RevisionScope,
)
from evidencedesk.retrieval.rerank import RankedEvidence

from evals.curation.build import ROOT, sha

# Authored fictional examples, not reviewed gold, real company policies or model outputs.
TOPICS = (
    (
        "policy",
        "How long does Lumen retain audit records?",
        "Lumen retains audit records for 30 days unless a legal hold applies.",
        "Lumen retains audit records for 90 days unless a legal hold applies.",
        "Lumen names audit record files using a date and a batch identifier.",
    ),
    (
        "technical",
        "How many requests per minute does the Lumen API permit?",
        "The Lumen API permits 120 requests per minute per workspace.",
        "The Lumen API permits 60 requests per minute per workspace.",
        "The Lumen API returns a request identifier in each response header.",
    ),
    (
        "support",
        "How long is the Lumen refund window for unused subscriptions?",
        "Lumen refunds unused subscriptions within 14 days of purchase.",
        "Lumen refunds unused subscriptions within 7 days of purchase.",
        "Lumen subscription receipts include the purchase date and account identifier.",
    ),
)


def uid(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, "evidencedesk-synthetic-context-v1/" + value)


@dataclass(frozen=True)
class Scenario:
    topic: str
    variant: str
    query: str
    ranked: RankedEvidence
    history: tuple[HistoryPair, ...]
    support_sha256: str
    conflict_sha256: str


def scenarios() -> tuple[Scenario, ...]:
    result = []
    workspace, user, kb = uid("workspace"), uid("user"), uid("kb")
    for topic, query, support, conflict, irrelevant in TOPICS:
        noises = [f"{irrelevant} Reference section {i}." for i in range(8)]
        variants = {
            "support_only": [support],
            "support_first": [support, *noises[:7]],
            "support_middle": [*noises[:3], support, *noises[3:7]],
            "support_last": [*noises[:7], support],
            "related_but_insufficient": noises[:7],
            "conflicting": [support, conflict],
            "stale_history": [support],
            "empty": [],
            "support_beyond_passage_limit": [*noises, support],
        }
        for variant, texts in variants.items():
            evidence = []
            refs = []
            for rank, text in enumerate(texts, 1):
                fingerprint = sha(text.encode())
                doc, rev, chunk = (
                    uid(topic + fingerprint + kind) for kind in ("doc", "rev", "chunk")
                )
                refs.append(RevisionScope(rev, doc, kb, fingerprint, 1))
                evidence.append(
                    Evidence(
                        Passage(
                            chunk,
                            doc,
                            rev,
                            kb,
                            f"Fictional Lumen {topic}",
                            text,
                            fingerprint,
                            1,
                            0,
                            None,
                            None,
                            0,
                            len(text),
                            (),
                            {},
                        ),
                        1 / rank,
                        rank,
                        None,
                        None,
                        None,
                    )
                )
            scope = ResolvedScope(
                workspace, user, (kb,), tuple(refs), "synthetic", "synthetic", 1, 1, "ask"
            )
            ordered = tuple(evidence)
            retrieval = RetrievalResult(
                ordered, scope, sha(query.encode()), "a" * 64, "hybrid_rerank", {}, {}, ()
            )
            history = (
                (
                    HistoryPair(
                        uid(topic + "history-user"),
                        uid(topic + "history-assistant"),
                        query,
                        conflict,
                    ),
                )
                if variant == "stale_history"
                else ()
            )
            result.append(
                Scenario(
                    topic,
                    variant,
                    query,
                    RankedEvidence(retrieval, ordered, (), None, 0, ()),
                    history,
                    sha(support.encode()),
                    sha(conflict.encode()),
                )
            )
    return tuple(result)


async def measure() -> dict[str, Any]:
    registry = {m.key: m for m in load_registry(ROOT / "contracts/config/models.yaml")}
    measurements = []
    for key in ("paid_standard", "local_standard"):
        model = registry[key]
        if key == "local_standard" and model.resolved_digest is None:
            raise ValueError("Local diagnostic requires a pinned model digest")
        adapter = (
            OpenAIAdapter("offline-unused")
            if key == "paid_standard"
            else OllamaAdapter(
                "http://127.0.0.1:11435",
                model.model_id,
                model.resolved_digest or "",
                LocalTokens(
                    ROOT / ".cache/models/qwen3-tokenizer",
                    ROOT / "contracts/config/local-tokenizer.lock.json",
                ),
            )
        )
        try:
            packer = context_packer(model, adapter)
            for scenario in scenarios():
                bundle = packer.pack(scenario.query, scenario.ranked, scenario.history)
                hashes = [e.passage.text_sha256 for e in bundle.evidence]
                request = bundle.request
                measurements.append(
                    {
                        "topic": scenario.topic,
                        "variant": scenario.variant,
                        "model_key": key,
                        "model_config_sha256": model.fingerprint,
                        "prompt_version": bundle.prompt_version,
                        "prompt_sha256": bundle.prompt_sha256,
                        "request_sha256": bundle.request_sha256,
                        "candidate_hashes": [
                            e.passage.text_sha256 for e in scenario.ranked.evidence
                        ],
                        "packed_hashes": hashes,
                        "support_in_packed_context": scenario.support_sha256 in hashes,
                        "conflict_in_packed_context": scenario.conflict_sha256 in hashes,
                        "support_evidence_label": f"E{hashes.index(scenario.support_sha256) + 1}"
                        if scenario.support_sha256 in hashes
                        else None,
                        "history_pairs": len(bundle.history_ids) // 2,
                        "input_tokens": bundle.input_count.tokens if bundle.input_count else None,
                        "count_method": bundle.input_count.method if bundle.input_count else None,
                        "input_estimated": bundle.input_count.estimated
                        if bundle.input_count
                        else None,
                        "evidence_tokens": bundle.evidence_tokens,
                        "fits_context": bundle.input_count.tokens
                        + request.max_output_tokens
                        + context_margin(request.context_cap)
                        <= request.context_cap
                        if request and bundle.input_count
                        else None,
                        "packer_abstention": bundle.abstention,
                    }
                )
        finally:
            await adapter.close()
    files = [
        "evals/runner/context_sensitivity.py",
        "apps/api/src/evidencedesk/ai/context.py",
        "apps/api/src/evidencedesk/ai/token_budget.py",
        "apps/api/src/evidencedesk/prompts/__init__.py",
        "apps/api/src/evidencedesk/prompts/grounded-assistant-v2.txt",
        "apps/api/src/evidencedesk/prompts/answer-schema-v2.txt",
        "contracts/config/models.yaml",
        "contracts/config/local-tokenizer.lock.json",
        "contracts/config/model-artifacts.lock.json",
        "uv.lock",
    ]
    return {
        "scope": "synthetic_context_packing_diagnostic",
        "quality_measured": False,
        "inference_calls": 0,
        "actual_api_spend_micro_usd": 0,
        "inputs_sha256": {name: sha((ROOT / name).read_bytes()) for name in files},
        "rows": measurements,
        "limitations": [
            "Three authored fictional topics, not representative or human-reviewed ground truth.",
            "Candidates are designed fixtures, not results of live retrieval or tenant authorization.",
            "Measures packing and label positions only; no semantic answer, conflict-resolution or injection-resistance score.",
            "Nonempty insufficient evidence still reaches generation; a packable request does not establish answerability.",
            "Each context variant needs its own frozen generation plan with identical context across candidate models; do not pool variants as independent questions.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output exists; choose a new immutable diagnostic path")
    report = asyncio.run(measure())
    with args.output.open("x") as stream:
        stream.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(f"Saved {len(report['rows'])} packing measurements; no inference or quality claim.")


if __name__ == "__main__":
    main()

"""Structural checks and a fail-closed human-review gate, separate from model evaluation."""

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from evals.curation.build import canonical, sha


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate JSON key")
        obj[key] = value
    return obj


def read_json(value: str) -> Any:
    return json.loads(
        value,
        object_pairs_hook=unique_object,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")),
    )


def rows(path: Path) -> list[dict[str, Any]]:
    if path.stat().st_size > 10_000_000:
        raise ValueError("Dataset file exceeds review-tool bound")
    result = [
        read_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not all(isinstance(row, dict) for row in result):
        raise ValueError("Expected JSON objects")
    return result


def case_hash(case: dict[str, Any]) -> str:
    return sha(canonical(case).encode())


def double_review_sample(cases: list[dict[str, Any]]) -> set[str]:
    # Stable stratified20% sample, chosen before anyone sees model candidates or reviewer answers.
    sample: set[str] = set()
    for split in ("development", "test"):
        for category in ("direct", "unanswerable", "conflict", "multi_document"):
            group = [c for c in cases if c["split"] == split and c["category"] == category]
            ordered = sorted(
                group, key=lambda c: sha(("double-review-v1:" + c["case_id"]).encode())
            )
            sample.update(c["case_id"] for c in ordered[: len(group) // 5])
    return sample


def review_gate(
    cases: list[dict[str, Any]], reviews: list[dict[str, Any]], *, single_reviewer: bool = False
) -> dict[str, Any]:
    identities = {c["case_id"]: case_hash(c) for c in cases}
    accepted: dict[str, set[str]] = {}
    disputed: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for review in reviews:
        key = (review["case_id"], review["reviewer_id"])
        if key in seen or identities.get(key[0]) != review["case_sha256"]:
            raise ValueError("Duplicate or stale review record")
        seen.add(key)
        if (
            review.get("human_attestation") is not True
            or review.get("rubric_version") != "human-review-v1"
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{2,79}", key[1])
            or review.get("decision") not in ("accept", "revise", "reject")
            or not review.get("reviewed_at")
            or not review.get("notes", " ").strip()
        ):
            raise ValueError(
                "Review must include human attestation, identity, date, decision and notes"
            )
        reviewed_at = datetime.fromisoformat(review["reviewed_at"].replace("Z", "+00:00"))
        if reviewed_at.tzinfo is None:
            raise ValueError("Review timestamp requires a timezone")
        if review["decision"] == "accept":
            accepted.setdefault(key[0], set()).add(key[1])
        else:
            disputed.add(key[0])
    missing = sorted(set(identities) - accepted.keys())
    sample = double_review_sample(cases)
    missing_second = sorted(uid for uid in sample if len(accepted.get(uid, set())) < 2)
    return {
        "ready": not missing and not disputed and (single_reviewer or not missing_second),
        "primary_accepted": len(identities) - len(missing),
        "required_primary": len(cases),
        "independently_reviewed": sum(len(v) >= 2 for v in accepted.values()),
        "required_second_sample": len(sample),
        "missing_primary": missing,
        "missing_second": missing_second,
        "disputed": sorted(disputed),
        "single_reviewer_limitation": single_reviewer,
    }


def validate(directory: Path, *, single_reviewer: bool = False) -> dict[str, Any]:
    root = directory.resolve()
    manifest = read_json((root / "manifest.json").read_text())
    if set(manifest["artifacts"]) != {"cases.jsonl", "adversarial.jsonl", "workflows.jsonl"}:
        raise ValueError("Manifest must hash every required dataset artifact")
    for filename, expected in manifest["artifacts"].items():
        path = (root / filename).resolve()
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or sha(path.read_bytes()) != expected
        ):
            raise ValueError("Dataset artifact hash or path mismatch")
    sources: dict[str, str] = {}
    document_rows = {}
    for doc in manifest["documents"]:
        path = (root / doc["path"]).resolve()
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or sha(path.read_bytes()) != doc["sha256"]
            or doc["id"] in sources
            or not doc.get("license")
            or doc.get("synthetic") is not True
        ):
            raise ValueError("Source integrity/license/path/identity failure")
        sources[doc["id"]] = path.read_text(encoding="utf-8")
        document_rows[doc["id"]] = doc
    cases = rows(root / "cases.jsonl")
    security = rows(root / "adversarial.jsonl")
    workflows = rows(root / "workflows.jsonl")
    if (len(cases), len(security), len(workflows)) != (400, 200, 50):
        raise ValueError("Required case counts differ")
    if len({c["case_id"] for c in cases + security + workflows}) != 650:
        raise ValueError("Duplicate case identifier")
    strata = Counter((c["split"], c["category"]) for c in cases)
    expected = {
        (split, category): n
        for split, counts in [("development", [60, 20, 10, 10]), ("test", [180, 60, 30, 30])]
        for category, n in zip(
            ["direct", "unanswerable", "conflict", "multi_document"], counts, strict=True
        )
    }
    if strata != expected:
        raise ValueError("Strata differ from the predeclared100/300 split")
    families: dict[str, str] = {}
    signatures: dict[str, str] = {}
    for case in cases:
        if families.setdefault(case["family"], case["split"]) != case["split"]:
            raise ValueError("Engineering family crosses development/test split")
        normalized = re.sub(r"\b[a-z]+-\d+\b|\d+", "<id>", case["query"].casefold())
        if signatures.setdefault(normalized, case["split"]) != case["split"]:
            raise ValueError("Normalized question duplicates across splits")
        if type(case["answerable"]) is not bool or not 1 <= len(case["query"]) <= 2000:
            raise ValueError("Invalid answerability/query")
        scope = case["scope"]
        if not scope["source_ids"] or len(set(scope["source_ids"])) != len(scope["source_ids"]):
            raise ValueError("Invalid source scope")
        for source_id in scope["source_ids"]:
            doc = document_rows[source_id]
            if doc["workspace"] != scope["workspace"] or doc["family"] != case["family"]:
                raise ValueError("Source is outside the case's authorized family/workspace")
        if (
            bool(case["gold_spans"]) != case["answerable"]
            or bool(case["expected_claims"]) != case["answerable"]
        ):
            raise ValueError("Missing gold or claims")
        if not case["answerable"] and not case["abstention_reason"]:
            raise ValueError("Unanswerable cases require a reason")
        for span in case["gold_spans"]:
            if (
                span["source_id"] not in scope["source_ids"]
                or type(span["start"]) is not int
                or type(span["end"]) is not int
                or not 0 <= span["start"] < span["end"] <= len(sources[span["source_id"]])
                or span["relevance"] not in (1, 2)
                or sources[span["source_id"]][span["start"] : span["end"]] != span["quote"]
            ):
                raise ValueError("Gold span is not an exact permitted source slice")
        if (
            case["category"] in ("conflict", "multi_document")
            and len({s["source_id"] for s in case["gold_spans"]}) < 2
        ):
            raise ValueError("Paired task lacks both evidence sources")
    known = {c["case_id"] for c in cases}
    if any(s["base_case_id"] not in known or not s["expected_forbidden_effects"] for s in security):
        raise ValueError("Invalid adversarial case linkage")
    if Counter(w["category"] for w in workflows) != {
        "comparison": 20,
        "approval_replay": 15,
        "failure_revocation": 15,
    }:
        raise ValueError("Invalid workflow strata")
    for workflow in workflows:
        if workflow["category"] == "comparison" and workflow["base_case_id"] not in known:
            raise ValueError("Invalid comparison workflow linkage")
    reviews = rows(root / "reviews.jsonl") if (root / "reviews.jsonl").exists() else []
    gate = review_gate(cases, reviews, single_reviewer=single_reviewer)
    return {
        "status": "structurally_valid",
        "dataset_sha256": sha((root / "manifest.json").read_bytes()),
        "counts": {
            "questions": len(cases),
            "security": len(security),
            "workflows": len(workflows),
            "documents": len(sources),
        },
        "split_family_overlap": 0,
        "normalized_question_overlap": 0,
        "cluster_count": len(families),
        "strata": {f"{split}/{category}": n for (split, category), n in sorted(strata.items())},
        "human_review": gate,
        "promotion_eligible": gate["ready"],
        "limitations": manifest["limitations"]
        + [
            "Lexical normalization checks are not a semantic near-duplicate audit; human leakage review is still required."
        ],
    }

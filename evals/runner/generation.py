"""Offline common-context generation scoring. Human judgments are inputs, never fabricated labels."""

from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals.curation.build import canonical, sha
from evals.curation.validate import case_hash, rows, validate
from evals.runner.statistics import paired_bootstrap, quantile, wilson

HASH = r"^[a-f0-9]{64}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Candidate(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    model_revision: str = Field(min_length=1, max_length=160)
    config_sha256: str = Field(pattern=HASH)
    execution: Literal["local", "hosted"]


class Plan(Strict):
    dataset_sha256: str = Field(pattern=HASH)
    source_snapshot_sha256: str = Field(pattern=HASH)
    split: Literal["development", "test"]
    privacy: Literal["public_synthetic", "local_only"]
    candidates: list[Candidate] = Field(min_length=2, max_length=6)
    case_contexts: dict[str, str] = Field(min_length=1, max_length=400)
    repetitions: Literal[3]
    budget_micro_usd: int = Field(ge=0)
    subset_reason: str | None = Field(max_length=500)

    @model_validator(mode="after")
    def valid_plan(self) -> Plan:
        import re

        if len({c.id for c in self.candidates}) != len(self.candidates):
            raise ValueError("Candidate identities must be unique")
        if any(not re.fullmatch(HASH, value) for value in self.case_contexts.values()):
            raise ValueError("Common evidence contexts need exact hashes")
        if self.privacy == "local_only" and any(c.execution != "local" for c in self.candidates):
            raise ValueError("Local-only evaluation cannot use hosted candidates")
        return self


class Generation(Strict):
    case_id: str = Field(min_length=1, max_length=120)
    case_sha256: str = Field(pattern=HASH)
    candidate_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    repetition: int = Field(ge=0, le=2)
    plan_sha256: str = Field(pattern=HASH)
    context_sha256: str = Field(pattern=HASH)
    model_revision: str = Field(min_length=1, max_length=160)
    config_sha256: str = Field(pattern=HASH)
    outcome: Literal["answered", "abstained", "error", "timeout", "invalid", "cancelled"]
    response_sha256: str | None = Field(pattern=HASH)
    latency_ms: float = Field(ge=0)
    ttft_ms: float | None = Field(ge=0)
    cost_micro_usd: int = Field(ge=0)
    authorized_citation_links: int = Field(ge=0)
    citation_links: int = Field(ge=0)

    @model_validator(mode="after")
    def coherent(self) -> Generation:
        if self.outcome in ("answered", "abstained") and self.response_sha256 is None:
            raise ValueError("Completed outputs require exact response identity")
        if self.authorized_citation_links > self.citation_links:
            raise ValueError("Authorized links exceed total links")
        if self.ttft_ms is not None and self.ttft_ms > self.latency_ms:
            raise ValueError("TTFT exceeds total latency")
        return self


class Judgment(Strict):
    observation_sha256: str = Field(pattern=HASH)
    rubric: Literal["human-review-v1"]
    reviewer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,79}$")
    human_attestation: bool
    reviewed_at: str
    claims: int = Field(ge=0)
    supported_claims: int = Field(ge=0)
    support_requiring_claims: int = Field(ge=0)
    correctly_cited_claims: int = Field(ge=0)
    supported_citation_links: int = Field(ge=0)
    relevance: int = Field(ge=0, le=2)
    completeness: int = Field(ge=0, le=2)
    abstention_correct: bool | None
    security_failure: bool

    @model_validator(mode="after")
    def checked_counts(self) -> Judgment:
        if not self.human_attestation or datetime.fromisoformat(self.reviewed_at).tzinfo is None:
            raise ValueError("Timezone-bearing attested human review required")
        if (
            self.supported_claims > self.claims
            or self.support_requiring_claims > self.claims
            or self.correctly_cited_claims
            > min(self.support_requiring_claims, self.supported_claims)
        ):
            raise ValueError("Invalid human claim denominators")
        return self


def identity(value: Strict) -> str:
    return sha(canonical(value.model_dump(mode="json")).encode("utf-8"))


def aggregate_generation(
    dataset: Path,
    plan: Plan,
    observations: list[Generation],
    judgments: list[Judgment],
    *,
    resamples: int = 10000,
) -> dict[str, Any]:
    validation = validate(dataset)
    if plan.dataset_sha256 != validation["dataset_sha256"]:
        raise ValueError("Generation plan dataset changed")
    available = {c["case_id"]: c for c in rows(dataset / "cases.jsonl") if c["split"] == plan.split}
    if not plan.case_contexts.keys() <= available.keys():
        raise ValueError("Unknown or wrong-split planned cases")
    if plan.case_contexts.keys() != available.keys() and not (plan.subset_reason or "").strip():
        raise ValueError("A subset needs a predeclared reason; never select only successes")
    cases = {uid: available[uid] for uid in plan.case_contexts}
    candidates = {c.id: c for c in plan.candidates}
    seen: dict[tuple[str, str, int], Generation] = {}
    by_hash: dict[str, Generation] = {}
    for row in observations:
        key = (row.case_id, row.candidate_id, row.repetition)
        candidate = candidates.get(row.candidate_id)
        case = cases.get(row.case_id)
        if case is None or candidate is None or key in seen:
            raise ValueError("Unknown or duplicate generation observation")
        if row.case_sha256 != case_hash(case) or row.plan_sha256 != identity(plan):
            raise ValueError("Stale case or experiment plan")
        if row.context_sha256 != plan.case_contexts[row.case_id]:
            raise ValueError("Candidates must use identical frozen evidence contexts")
        if (
            row.model_revision != candidate.model_revision
            or row.config_sha256 != candidate.config_sha256
        ):
            raise ValueError("Model revision or configuration changed")
        seen[key] = row
        by_hash[identity(row)] = row
    labels = {}
    for judgment in judgments:
        observation = by_hash.get(judgment.observation_sha256)
        if observation is None or judgment.observation_sha256 in labels:
            raise ValueError("Unknown/stale or disputed duplicate judgment; adjudicate offline")
        if observation.outcome not in ("answered", "abstained"):
            raise ValueError("Failed/invalid responses cannot receive semantic labels")
        if judgment.supported_citation_links > observation.authorized_citation_links:
            raise ValueError("Semantic citations cannot exceed authorized links")
        abstained = observation.outcome == "abstained"
        if abstained != (judgment.abstention_correct is not None):
            raise ValueError("Abstention verdict must match the output kind")
        if abstained and (judgment.claims or observation.citation_links):
            raise ValueError("Abstentions cannot inflate claim/citation metrics")
        labels[judgment.observation_sha256] = judgment
    results: dict[str, Any] = {}
    paired: dict[str, dict[str, list[float]]] = {}
    for cid in candidates:
        outcomes: Counter[str] = Counter()
        confusion: Counter[str] = Counter()
        totals: Counter[str] = Counter()
        latencies, ttfts = [], []
        scores: dict[str, list[float]] = {}
        review_complete = True
        first_successes = 0
        for uid, case in cases.items():
            scores[uid] = []
            for repetition in range(plan.repetitions):
                observation = seen.get((uid, cid, repetition))
                outcome = observation.outcome if observation else "missing"
                outcomes[outcome] += 1
                truth = "answerable" if case["answerable"] else "unanswerable"
                confusion[f"{truth}_{outcome}"] += 1
                success = False
                if observation:
                    totals["cost_micro_usd"] += observation.cost_micro_usd
                    totals["citation_links"] += observation.citation_links
                    totals["authorized_links"] += observation.authorized_citation_links
                    latencies.append(observation.latency_ms)
                    if observation.ttft_ms is not None:
                        ttfts.append(observation.ttft_ms)
                    review = labels.get(identity(observation))
                    if outcome in ("answered", "abstained") and review is None:
                        review_complete = False
                    if review:
                        totals["reviewed_outputs"] += 1
                        totals["claims"] += review.claims
                        totals["supported_claims"] += review.supported_claims
                        totals["required_claims"] += review.support_requiring_claims
                        totals["cited_claims"] += review.correctly_cited_claims
                        totals["reviewed_links"] += observation.citation_links
                        totals["supported_links"] += review.supported_citation_links
                        totals["security_failures"] += int(review.security_failure)
                        if outcome == "abstained" and not case["answerable"]:
                            totals["correct_abstentions"] += int(review.abstention_correct is True)
                        success = (
                            not review.security_failure
                            and observation.authorized_citation_links == observation.citation_links
                            and (
                                (
                                    case["answerable"]
                                    and outcome == "answered"
                                    and review.claims > 0
                                    and review.relevance == 2
                                    and review.completeness == 2
                                    and review.supported_claims == review.claims
                                    and review.correctly_cited_claims
                                    == review.support_requiring_claims
                                    and review.supported_citation_links
                                    == observation.citation_links
                                )
                                or (
                                    not case["answerable"]
                                    and outcome == "abstained"
                                    and review.abstention_correct is True
                                )
                            )
                        )
                scores[uid].append(float(success))
                if repetition == 0:
                    first_successes += int(success)

        def ratio(numerator: int, denominator: int) -> dict[str, float | int | None]:
            return {
                "numerator": numerator,
                "n": denominator,
                "value": numerator / denominator if denominator else None,
            }

        completed = outcomes["answered"] + outcomes["abstained"]
        answerable_n = sum(c["answerable"] for c in cases.values()) * plan.repetitions
        unanswerable_n = len(cases) * plan.repetitions - answerable_n
        # Claim/link measurements cover reviewed outputs only; never imply missing reviews are supported.
        results[cid] = {
            "declared_questions": len(cases),
            "declared_generations": len(cases) * plan.repetitions,
            "outcomes": dict(outcomes),
            "answerability_confusion": dict(confusion),
            "review_coverage": ratio(totals["reviewed_outputs"], completed),
            "completion": ratio(completed, len(cases) * plan.repetitions),
            "supported_claims": ratio(totals["supported_claims"], totals["claims"]),
            "citation_precision": ratio(totals["supported_links"], totals["reviewed_links"]),
            "citation_recall": ratio(totals["cited_claims"], totals["required_claims"]),
            "citation_authorization": ratio(totals["authorized_links"], totals["citation_links"]),
            "abstention_recall": ratio(totals["correct_abstentions"], unanswerable_n)
            if review_complete
            else {"numerator": None, "n": unanswerable_n, "value": None},
            "false_abstention_rate": ratio(confusion["answerable_abstained"], answerable_n),
            "task_success_first_repetition": asdict(wilson(first_successes, len(cases)))
            if review_complete
            else asdict(wilson(0, 0)),
            "task_success_mean_repetitions": fmean(fmean(v) for v in scores.values())
            if review_complete
            else None,
            "semantic_review_complete": review_complete,
            "latency_ms": {
                "n": len(latencies),
                **{
                    name: quantile(latencies, q) if latencies else None
                    for name, q in [("p50", 0.5), ("p95", 0.95), ("p99", 0.99)]
                },
            },
            "ttft_ms": {"n": len(ttfts), "p50": quantile(ttfts, 0.5) if ttfts else None},
            "recorded_cost_micro_usd": totals["cost_micro_usd"],
            "security_failures": totals["security_failures"],
        }
        if review_complete:
            paired[cid] = scores
    baseline = plan.candidates[0].id
    deltas = {
        cid: asdict(
            paired_bootstrap(
                paired[baseline],
                paired[cid],
                {uid: c["family"] for uid, c in cases.items()},
                resamples=resamples,
            )
        )
        for cid in candidates
        if cid != baseline and baseline in paired and cid in paired
    }
    cost = sum(r.cost_micro_usd for r in observations)
    return {
        "report_version": 1,
        "scope": "recorded_generation_diagnostic",
        "task_success_rubric": "fully-supported-citations-v2",
        "plan_sha256": identity(plan),
        "dataset_sha256": validation["dataset_sha256"],
        "source_snapshot_sha256": plan.source_snapshot_sha256,
        "candidate_results": results,
        "paired_task_success_delta": deltas,
        "subset_strata": dict(Counter(c["category"] for c in cases.values())),
        "recorded_cost_micro_usd": cost,
        "budget_exceeded": cost > plan.budget_micro_usd,
        "cost_coverage_complete": len(observations)
        == len(cases) * len(candidates) * plan.repetitions,
        "promotion_eligible": False,
        "limitations": [
            "Offline records and human attestations are not independently authenticated.",
            "Claim/link metrics cover reviewed outputs only; task-success comparison requires all completed outputs reviewed.",
            "First-repetition Wilson uses questions, not correlated repetitions; paired bootstrap clusters repetitions within questions and archetypes.",
            "Hosted judges are not invoked. Independent second-review reliability and judge calibration remain unmeasured.",
            "No runtime collection, spend enforcement or semantic model superiority is established by this report.",
            *validation["limitations"],
        ],
    }


def main() -> None:
    import argparse
    import json

    from evals.curation.build import ROOT
    from evals.curation.validate import read_json
    from evals.runner.provenance import verify

    parser = argparse.ArgumentParser(
        description="Aggregate recorded common-context generation and human judgments; performs no inference"
    )
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/datasets/benchmark-v1-draft")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output exists; choose a new immutable report path")
    if args.plan.stat().st_size > 1024 * 1024:
        parser.error("Plan exceeds 1 MiB")
    plan = Plan.model_validate(read_json(args.plan.read_text()))
    source_hash, _ = verify(args.source_manifest)
    if plan.source_snapshot_sha256 != source_hash:
        parser.error("Plan does not match the verified source snapshot")
    result = aggregate_generation(
        args.dataset,
        plan,
        [Generation.model_validate(row) for row in rows(args.observations)],
        [Judgment.model_validate(row) for row in rows(args.judgments)],
    )
    result["observations_sha256"] = sha(args.observations.read_bytes())
    result["judgments_sha256"] = sha(args.judgments.read_bytes())
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"scope": result["scope"], "promotion_eligible": False}))


if __name__ == "__main__":
    main()

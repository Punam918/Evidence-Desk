"""Aggregate recorded retrieval observations; missing/failed cases remain in declared denominators."""

from collections import Counter
from dataclasses import asdict
from pathlib import Path
from statistics import fmean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals.curation.build import sha
from evals.curation.validate import case_hash, read_json, rows, validate
from evals.runner.metrics import Span, precision_at, recall_at, reciprocal_rank
from evals.runner.statistics import paired_bootstrap, quantile, wilson

Variant = Literal["dense", "hybrid", "hybrid_rerank", "hybrid_rewrite"]
VARIANTS: tuple[Variant, ...] = ("dense", "hybrid", "hybrid_rerank", "hybrid_rewrite")


class RetrievedSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str = Field(min_length=1, max_length=120)
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def nonempty(self) -> RetrievedSpan:
        if self.start >= self.end:
            raise ValueError("Empty retrieved span")
        return self


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    case_id: str = Field(min_length=1, max_length=120)
    case_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    variant: Variant
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    collection_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["completed", "error", "timeout", "cancelled"]
    error_code: (
        Literal[
            "index_unavailable",
            "source_unavailable",
            "deadline_exceeded",
            "cancelled",
            "invalid_result",
            "provider_unavailable",
        ]
        | None
    )
    retrieved: list[RetrievedSpan] = Field(max_length=20)
    latency_ms: float | None = Field(ge=0)
    cost_micro_usd: int = Field(ge=0)

    @model_validator(mode="after")
    def terminal(self) -> Observation:
        if (self.status == "completed") != (self.error_code is None):
            raise ValueError("Status and error must agree")
        return self


def aggregate(
    dataset: Path,
    observations: Path,
    *,
    split: Literal["development", "test"] = "development",
    allow_draft_diagnostic: bool = False,
    freeze: Path | None = None,
    resamples: int = 10000,
    seed: int = 20261010,
) -> dict[str, Any]:
    validation = validate(dataset)
    manifest = read_json((dataset / "manifest.json").read_text())
    if not allow_draft_diagnostic:
        if freeze is None:
            raise ValueError("A human-reviewed freeze is required; draft mode must be explicit")
        envelope = read_json(freeze.read_text())
        checked = validate(
            dataset, single_reviewer=bool(envelope["human_review"]["single_reviewer_limitation"])
        )
        if (
            not checked["promotion_eligible"]
            or envelope["status"] != "human_reviewed_freeze"
            or envelope["dataset_sha256"] != checked["dataset_sha256"]
            or envelope["reviews_sha256"] != sha((dataset / "reviews.jsonl").read_bytes())
        ):
            raise ValueError("Reviewed freeze is stale, disputed or incomplete")
    cases = {c["case_id"]: c for c in rows(dataset / "cases.jsonl") if c["split"] == split}
    parsed = [Observation.model_validate(o) for o in rows(observations)]
    seen: dict[tuple[str, str], Observation] = {}
    configs: dict[str, str] = {}
    collection: str | None = None
    source_snapshot: str | None = None
    documents = {d["id"]: d for d in manifest["documents"]}
    lengths = {
        uid: len((dataset / d["path"]).read_text(encoding="utf-8")) for uid, d in documents.items()
    }
    for record in parsed:
        case = cases.get(record.case_id)
        identity = (record.case_id, record.variant)
        if case is None or case_hash(case) != record.case_sha256 or identity in seen:
            raise ValueError("Unknown, stale, wrong-split or duplicate record")
        if configs.setdefault(record.variant, record.config_sha256) != record.config_sha256:
            raise ValueError("Variant configuration changed within the experiment")
        collection = collection or record.collection_sha256
        if collection != record.collection_sha256:
            raise ValueError("Paired variants must use the same frozen collection")
        source_snapshot = source_snapshot or record.source_snapshot_sha256
        if source_snapshot != record.source_snapshot_sha256:
            raise ValueError("Paired variants must use the same source snapshot")
        for chunk in record.retrieved:
            if (
                chunk.source_id not in case["scope"]["source_ids"]
                or chunk.end > lengths[chunk.source_id]
            ):
                raise ValueError("Observation contains unauthorized or invalid source coordinates")
        seen[identity] = record
    families = {uid: c["family"] for uid, c in cases.items() if c["answerable"]}
    per_variant: dict[str, Any] = {}
    values: dict[str, dict[str, dict[str, list[float]]]] = {}
    for variant in VARIANTS:
        scores: dict[str, dict[str, list[float]]] = {
            name: {} for name in ("recall_at_20", "precision_at_8", "mrr_at_20")
        }
        outcomes: Counter[str] = Counter()
        latencies = []
        cost = 0
        for uid, case in cases.items():
            observation = seen.get((uid, variant))
            outcomes[observation.status if observation else "missing"] += 1
            if observation:
                cost += observation.cost_micro_usd
                if observation.latency_ms is not None:
                    latencies.append(observation.latency_ms)
            if not case["answerable"]:
                continue
            gold = [
                Span(g["source_id"], g["start"], g["end"], g["relevance"])
                for g in case["gold_spans"]
            ]
            # Failed/absent retrievals score zero. Never select only successful outcomes.
            retrieved = (
                [Span(c.source_id, c.start, c.end) for c in observation.retrieved]
                if observation and observation.status == "completed"
                else []
            )
            measured = {
                "recall_at_20": recall_at(gold, retrieved, 20),
                "precision_at_8": precision_at(gold, retrieved, 8),
                "mrr_at_20": reciprocal_rank(gold, retrieved),
            }
            for name, value in measured.items():
                assert value is not None
                scores[name][uid] = [value]
        metrics = {
            name: {
                "value": fmean(v[0] for v in casescores.values()) if casescores else None,
                "n": len(casescores),
            }
            for name, casescores in scores.items()
        }
        metrics["ndcg_at_10"] = {
            "value": None,
            "n": 0,
        }  # No reviewed candidate pool supplied to this command.
        per_variant[variant] = {
            "declared_cases": len(cases),
            "observed_cases": sum(outcomes.values()) - outcomes["missing"],
            "outcomes": dict(outcomes),
            "retrieval_completion": asdict(wilson(outcomes["completed"], len(cases))),
            "metrics": metrics,
            "latency_ms": {
                "n": len(latencies),
                **{
                    q: quantile(latencies, p) if latencies else None
                    for q, p in [("p50", 0.5), ("p95", 0.95), ("p99", 0.99)]
                },
            },
            "recorded_cost_micro_usd": cost,
            "config_sha256": configs.get(variant),
        }
        values[variant] = scores
    paired = {
        variant: {
            name: asdict(
                paired_bootstrap(
                    values["dense"][name],
                    values[variant][name],
                    families,
                    resamples=resamples,
                    seed=seed,
                )
            )
            for name in values["dense"]
        }
        for variant in VARIANTS[1:]
    }
    return {
        "report_version": 1,
        "scope": "synthetic_draft_diagnostic"
        if allow_draft_diagnostic
        else "reviewed_recorded_retrieval",
        "status": "completed" if len(seen) == len(cases) * len(VARIANTS) else "incomplete",
        "dataset_version": manifest["version"],
        "dataset_sha256": validation["dataset_sha256"],
        "observations_sha256": sha(observations.read_bytes()),
        "split": split,
        "variants": per_variant,
        "paired_delta_vs_dense": paired,
        "collection_sha256": collection,
        "source_snapshot_sha256": source_snapshot,
        "bootstrap": {
            "resamples": resamples,
            "seed": seed,
            "unit": "archetype_then_question",
            "confidence": 0.95,
        },
        "semantic_generation": "not_measured",
        "promotion_eligible": False,
        "limitations": [
            "Recorded transports are not independently authenticated by this aggregator.",
            "Missing observations remain errors and count in retrieval-quality denominators.",
            "nDCG needs a separately reviewed candidate relevance pool; no value is invented.",
            "No live generation, citation semantics, privacy-hosted judge or model superiority is established.",
            *validation["limitations"],
        ],
    }

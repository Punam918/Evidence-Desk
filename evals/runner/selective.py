"""Offline answer-selection diagnostics; scores are not probabilities or serving policy."""

from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from evals.curation.validate import rows
from evals.runner.generation import (
    HASH,
    Generation,
    Judgment,
    Plan,
    Strict,
    aggregate_generation,
    identity,
)


class SelectionPlan(Strict):
    generation_plan_sha256: str = Field(pattern=HASH)
    scorer_sha256: str = Field(pattern=HASH)
    thresholds: list[float] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def threshold_grid(self) -> SelectionPlan:
        if self.thresholds != sorted(set(self.thresholds)) or any(
            not 0 <= threshold <= 1 for threshold in self.thresholds
        ):
            raise ValueError("Declare distinct increasing thresholds in [0,1]")
        return self


class SelectionScore(Strict):
    observation_sha256: str = Field(pattern=HASH)
    scorer_sha256: str = Field(pattern=HASH)
    score: float = Field(ge=0, le=1)


def aggregate_selection(
    dataset: Path,
    plan: Plan,
    selection: SelectionPlan,
    observations: list[Generation],
    judgments: list[Judgment],
    scores: list[SelectionScore],
) -> dict[str, Any]:
    if selection.generation_plan_sha256 != identity(plan):
        raise ValueError("Selection plan does not match generation plan")
    # Reuse all existing case/config/context/review/duplicate/privacy validations.
    baseline = aggregate_generation(dataset, plan, observations, judgments, resamples=100)
    by_hash = {identity(row): row for row in observations}
    scored: dict[str, float] = {}
    for score in scores:
        row = by_hash.get(score.observation_sha256)
        if (
            row is None
            or row.outcome != "answered"
            or score.observation_sha256 in scored
            or score.scorer_sha256 != selection.scorer_sha256
        ):
            raise ValueError("Unknown, non-answer, duplicate or changed-scorer observation")
        scored[score.observation_sha256] = score.score
    labels = {judgment.observation_sha256: judgment for judgment in judgments}
    cases = {
        row["case_id"]: row
        for row in rows(dataset / "cases.jsonl")
        if row["case_id"] in plan.case_contexts
    }
    candidates = {}
    for candidate in plan.candidates:
        observed = [row for row in observations if row.candidate_id == candidate.id]
        outcomes = Counter(row.outcome for row in observed)
        planned = len(cases) * plan.repetitions
        points = []
        for threshold in selection.thresholds:
            selected = reviewed = bad = withheld = unscored = withheld_good = 0
            for row in observed:
                if row.outcome != "answered":
                    continue
                key = identity(row)
                score_value = scored.get(key)
                if score_value is None:
                    unscored += 1
                    continue
                review = labels.get(key)
                good = review is not None and (
                    cases[row.case_id]["answerable"]
                    and review.claims > 0
                    and review.supported_claims == review.claims
                    and review.correctly_cited_claims == review.support_requiring_claims
                    and review.supported_citation_links == row.citation_links
                    and row.authorized_citation_links == row.citation_links
                    and review.relevance == review.completeness == 2
                    and not review.security_failure
                )
                # A whole score tie is selected together. No arbitrary top-k tie break.
                if score_value >= threshold:
                    selected += 1
                    reviewed += review is not None
                    bad += review is not None and not good
                else:
                    withheld += 1
                    withheld_good += good
            unknown = selected - reviewed
            points.append(
                {
                    "threshold": threshold,
                    "planned_attempts": planned,
                    "selected_answers": selected,
                    "withheld_answers": withheld,
                    "withheld_reviewed_good_answers": withheld_good,
                    "unscored_answers": unscored,
                    "native_abstentions": outcomes["abstained"],
                    "failed_attempts": len(observed) - outcomes["answered"] - outcomes["abstained"],
                    "missing_attempts": planned - len(observed),
                    "answer_coverage": selected / planned,
                    "selected_reviewed": reviewed,
                    "selected_unreviewed": unknown,
                    "reviewed_rubric_failures": bad,
                    "reviewed_risk": bad / reviewed if reviewed else None,
                    "risk_lower_bound": bad / selected if selected else None,
                    "risk_upper_bound": (bad + unknown) / selected if selected else None,
                }
            )
        candidates[candidate.id] = {
            "points": points,
            "security_failures_across_all_reviewed_outputs": sum(
                labels[identity(row)].security_failure
                for row in observed
                if identity(row) in labels
            ),
        }
    return {
        "report_version": 1,
        "scope": "recorded_selective_answer_diagnostic",
        "generation_plan_sha256": identity(plan),
        "selection_plan_sha256": identity(selection),
        "scorer_sha256": selection.scorer_sha256,
        "declared_questions": len(cases),
        "independent_archetypes": len({case["family"] for case in cases.values()}),
        "candidate_results": candidates,
        "recorded_cost_micro_usd": baseline["recorded_cost_micro_usd"],
        "budget_exceeded": baseline["budget_exceeded"],
        "promotion_eligible": False,
        "selected_threshold": None,
        "limitations": [
            "Scores are ordering signals, not calibrated confidence probabilities.",
            "Risk is failure of the declared human support/completeness/citation rubric, not independently verified truth.",
            "Risk bounds cover unknown judgments, not sampling uncertainty; correlated repetitions are not independent observations.",
            "No threshold is chosen here. Freeze tuning on development data before held-out evaluation.",
            "Withholding after generation saves no generation cost; this is a recorded policy diagnostic, not a serving or spend claim.",
            *baseline["limitations"],
        ],
    }


def main() -> None:
    import argparse
    import json

    from evals.curation.build import ROOT, sha
    from evals.curation.validate import read_json
    from evals.runner.provenance import verify

    parser = argparse.ArgumentParser(
        description="Evaluate recorded answer-selection risk/coverage; no inference"
    )
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/datasets/benchmark-v1-draft")
    for name in (
        "plan",
        "selection-plan",
        "observations",
        "judgments",
        "scores",
        "source-manifest",
        "output",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output exists; choose a new immutable report path")
    if max(args.plan.stat().st_size, args.selection_plan.stat().st_size) > 1024 * 1024:
        parser.error("Plan exceeds1 MiB")
    plan = Plan.model_validate(read_json(args.plan.read_text()))
    selection = SelectionPlan.model_validate(read_json(args.selection_plan.read_text()))
    source_hash, _ = verify(args.source_manifest)
    if plan.source_snapshot_sha256 != source_hash:
        parser.error("Plan does not match the verified source snapshot")
    result = aggregate_selection(
        args.dataset,
        plan,
        selection,
        [Generation.model_validate(row) for row in rows(args.observations)],
        [Judgment.model_validate(row) for row in rows(args.judgments)],
        [SelectionScore.model_validate(row) for row in rows(args.scores)],
    )
    result.update(
        {
            name + "_sha256": sha(getattr(args, name).read_bytes())
            for name in ("observations", "judgments", "scores")
        }
    )
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"scope": result["scope"], "promotion_eligible": False}))


if __name__ == "__main__":
    main()

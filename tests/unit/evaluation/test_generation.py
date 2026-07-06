"""Invented judgments are transient test fixtures, never benchmark review artifacts."""

import json
import subprocess
import sys

import pytest

from evals.curation.build import sha
from evals.curation.validate import case_hash, rows, validate
from evals.runner.generation import Generation, Judgment, Plan, aggregate_generation, identity
from evals.runner.provenance import snapshot
from tests.unit.evaluation.test_recorded import DATASET


@pytest.fixture
def experiment():
    cases = rows(DATASET / "cases.jsonl")
    selected = [cases[0], next(c for c in cases if not c["answerable"])]
    plan = Plan.model_validate(
        {
            "dataset_sha256": validate(DATASET)["dataset_sha256"],
            "source_snapshot_sha256": "a" * 64,
            "split": "development",
            "privacy": "public_synthetic",
            "repetitions": 3,
            "budget_micro_usd": 5,
            "subset_reason": "Small deterministic unit-test fixture, not a benchmark",
            "case_contexts": {c["case_id"]: "b" * 64 for c in selected},
            "candidates": [
                {
                    "id": cid,
                    "model_revision": cid + "-dated-test-snapshot",
                    "config_sha256": "c" * 64,
                    "execution": execution,
                }
                for cid, execution in [("baseline", "local"), ("challenger", "hosted")]
            ],
        }
    )
    observations, judgments = [], []
    for candidate in plan.candidates:
        for case in selected:
            for repetition in range(3):
                answered = case["answerable"]
                row = Generation(
                    case_id=case["case_id"],
                    case_sha256=case_hash(case),
                    candidate_id=candidate.id,
                    repetition=repetition,
                    plan_sha256=identity(plan),
                    context_sha256="b" * 64,
                    model_revision=candidate.model_revision,
                    config_sha256=candidate.config_sha256,
                    outcome="answered" if answered else "abstained",
                    response_sha256="d" * 64,
                    latency_ms=20.0,
                    ttft_ms=10.0,
                    cost_micro_usd=1,
                    authorized_citation_links=int(answered),
                    citation_links=int(answered),
                )
                observations.append(row)
                judgments.append(
                    Judgment(
                        observation_sha256=identity(row),
                        rubric="human-review-v1",
                        reviewer_id="TEST-ONLY",
                        human_attestation=True,
                        reviewed_at="2026-10-12T00:00:00Z",
                        claims=int(answered),
                        supported_claims=int(answered),
                        support_requiring_claims=int(answered),
                        correctly_cited_claims=int(answered),
                        supported_citation_links=int(answered),
                        relevance=2,
                        completeness=2,
                        abstention_correct=None if answered else True,
                        security_failure=False,
                    )
                )
    return plan, observations, judgments


def test_repetitions_are_clustered_and_human_semantics_explicit(experiment):
    plan, observations, labels = experiment
    result = aggregate_generation(DATASET, plan, observations, labels, resamples=100)
    baseline = result["candidate_results"]["baseline"]
    assert baseline["task_success_first_repetition"]["n"] == 2
    assert baseline["declared_generations"] == 6
    assert baseline["supported_claims"] == {"numerator": 3, "n": 3, "value": 1.0}
    assert baseline["review_coverage"]["n"] == 6
    assert baseline["abstention_recall"] == {"numerator": 3, "n": 3, "value": 1.0}
    assert baseline["false_abstention_rate"]["value"] == 0
    assert result["paired_task_success_delta"]["challenger"]["n"] == 2
    assert result["paired_task_success_delta"]["challenger"]["value"] == 0
    assert result["recorded_cost_micro_usd"] == 12 and result["budget_exceeded"] is True
    assert result["promotion_eligible"] is False
    assert result == aggregate_generation(DATASET, plan, observations, labels, resamples=100)


def test_unreviewed_outputs_never_become_semantic_successes(experiment):
    plan, observations, _ = experiment
    report = aggregate_generation(DATASET, plan, observations, [], resamples=100)
    baseline = report["candidate_results"]["baseline"]
    assert baseline["completion"]["value"] == 1
    assert baseline["supported_claims"]["value"] is None
    assert baseline["task_success_first_repetition"]["value"] is None
    assert baseline["abstention_recall"]["value"] is None
    assert baseline["semantic_review_complete"] is False
    assert report["paired_task_success_delta"] == {}


def test_extra_unsupported_citation_is_not_a_success_despite_complete_claim_support(experiment):
    plan, observations, labels = experiment
    original = observations[0]
    observations[0] = Generation.model_validate(
        {
            **original.model_dump(),
            "citation_links": 2,
            "authorized_citation_links": 2,
        }
    )
    labels[0] = Judgment.model_validate(
        {
            **labels[0].model_dump(),
            "observation_sha256": identity(observations[0]),
        }
    )
    report = aggregate_generation(DATASET, plan, observations, labels, resamples=100)
    baseline = report["candidate_results"]["baseline"]
    assert report["task_success_rubric"] == "fully-supported-citations-v2"
    assert baseline["supported_claims"]["value"] == 1.0
    assert baseline["citation_recall"]["value"] == 1.0
    assert baseline["citation_authorization"]["value"] == 1.0
    assert baseline["citation_precision"]["value"] == 3 / 4
    assert baseline["task_success_first_repetition"]["value"] == 0.5
    assert baseline["task_success_mean_repetitions"] == pytest.approx(5 / 6)


def test_failures_and_missing_keep_full_denominators_latency_and_cost(experiment):
    plan, observations, labels = experiment
    original = observations.pop(0)
    labels = [label for label in labels if label.observation_sha256 != identity(original)]
    failed = observations[0]
    labels = [label for label in labels if label.observation_sha256 != identity(failed)]
    observations[0] = Generation.model_validate(
        {**failed.model_dump(), "outcome": "timeout", "latency_ms": 1000.0, "response_sha256": None}
    )
    report = aggregate_generation(DATASET, plan, observations, labels, resamples=100)
    base = report["candidate_results"]["baseline"]
    assert base["completion"] == {"numerator": 4, "n": 6, "value": 4 / 6}
    assert base["outcomes"]["missing"] == base["outcomes"]["timeout"] == 1
    assert base["latency_ms"]["n"] == 5 and base["latency_ms"]["p95"] > 500
    assert base["task_success_first_repetition"]["n"] == 2
    assert base["task_success_first_repetition"]["value"] == 0.5
    assert report["cost_coverage_complete"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "context",
        "config",
        "model",
        "plan",
        "case",
        "duplicate",
        "stale_label",
        "duplicate_label",
        "failure_label",
        "citation_label",
    ],
)
def test_invalid_experiments_fail_closed(experiment, fault):
    plan, observations, labels = experiment
    if fault in ("context", "config", "model", "plan", "case"):
        field = {
            "context": "context_sha256",
            "config": "config_sha256",
            "model": "model_revision",
            "plan": "plan_sha256",
            "case": "case_sha256",
        }[fault]
        observations[0] = Generation.model_validate(
            {**observations[0].model_dump(), field: "f" * 64}
        )
    elif fault == "duplicate":
        observations.append(observations[0])
    elif fault == "stale_label":
        labels[0] = Judgment.model_validate(
            {**labels[0].model_dump(), "observation_sha256": "f" * 64}
        )
    elif fault == "duplicate_label":
        labels.append(labels[0])
    elif fault == "failure_label":
        observations[0] = Generation.model_validate(
            {**observations[0].model_dump(), "outcome": "error"}
        )
        labels[0] = Judgment.model_validate(
            {**labels[0].model_dump(), "observation_sha256": identity(observations[0])}
        )
    elif fault == "citation_label":
        labels[0] = Judgment.model_validate(
            {**labels[0].model_dump(), "supported_citation_links": 2}
        )
    with pytest.raises(ValueError):
        aggregate_generation(DATASET, plan, observations, labels, resamples=100)


def test_local_only_plans_and_posthoc_subsets_rejected(experiment):
    plan, observations, labels = experiment
    with pytest.raises(ValueError, match="Local-only"):
        Plan.model_validate({**plan.model_dump(), "privacy": "local_only"})
    without_reason = Plan.model_validate({**plan.model_dump(), "subset_reason": None})
    with pytest.raises(ValueError, match="predeclared"):
        aggregate_generation(DATASET, without_reason, observations, labels)


@pytest.mark.parametrize(
    "field,value",
    [
        ("human_attestation", False),
        ("reviewed_at", "2026-10-12"),
        ("supported_claims", 99),
        ("correctly_cited_claims", 99),
    ],
)
def test_review_attestation_and_counts_required(experiment, field, value):
    _, _, labels = experiment
    with pytest.raises(ValueError):
        Judgment.model_validate({**labels[0].model_dump(), field: value})


def test_generation_cli_is_offline_hash_bound_and_refuses_overwrite(experiment, tmp_path):
    plan, _, _ = experiment
    source = tmp_path / "source.json"
    source.write_text(json.dumps(snapshot()))
    plan = Plan.model_validate(
        {**plan.model_dump(), "source_snapshot_sha256": sha(source.read_bytes())}
    )
    plan_path, observations, judgments, output = [
        tmp_path / name
        for name in ("plan.json", "observations.jsonl", "judgments.jsonl", "report.json")
    ]
    plan_path.write_text(plan.model_dump_json())
    observations.write_text("")
    judgments.write_text("")
    command = [
        sys.executable,
        "-m",
        "evals.runner.generation",
        "--plan",
        str(plan_path),
        "--observations",
        str(observations),
        "--judgments",
        str(judgments),
        "--source-manifest",
        str(source),
        "--output",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["candidate_results"]["baseline"]["outcomes"] == {"missing": 6}
    assert report["promotion_eligible"] is False and report["cost_coverage_complete"] is False
    assert subprocess.run(command, capture_output=True, timeout=30).returncode != 0

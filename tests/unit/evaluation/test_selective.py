"""Synthetic test judgments never enter the reviewed benchmark dataset."""

import json
import subprocess
import sys

import pytest

from evals.curation.build import sha
from evals.runner.generation import Generation, Judgment, Plan, identity
from evals.runner.provenance import snapshot, verify
from evals.runner.selective import SelectionPlan, SelectionScore, aggregate_selection
from tests.unit.evaluation.test_export import source_tree
from tests.unit.evaluation.test_generation import experiment as experiment
from tests.unit.evaluation.test_recorded import DATASET


def policy(plan):
    return SelectionPlan(
        generation_plan_sha256=identity(plan), scorer_sha256="e" * 64, thresholds=[0.0, 0.5, 1.0]
    )


def signals(observations):
    return [
        SelectionScore(observation_sha256=identity(row), scorer_sha256="e" * 64, score=0.5)
        for row in observations
        if row.outcome == "answered"
    ]


def test_ties_coverage_withheld_good_and_no_automatic_threshold_selection(experiment):
    plan, observed, labels = experiment
    report = aggregate_selection(DATASET, plan, policy(plan), observed, labels, signals(observed))
    first, tied, withheld = report["candidate_results"]["baseline"]["points"]
    assert first["selected_answers"] == tied["selected_answers"] == 3
    assert tied["answer_coverage"] == 0.5 and tied["planned_attempts"] == 6
    assert tied["reviewed_risk"] == tied["risk_upper_bound"] == 0
    assert withheld["selected_answers"] == 0 and withheld["risk_upper_bound"] is None
    assert withheld["withheld_reviewed_good_answers"] == 3
    assert report["promotion_eligible"] is False and report["selected_threshold"] is None
    assert report["budget_exceeded"] is True


def test_unknown_reviews_are_explicit_risk_bounds_not_perfect_quality(experiment):
    plan, observed, _ = experiment
    report = aggregate_selection(DATASET, plan, policy(plan), observed, [], signals(observed))
    point = report["candidate_results"]["baseline"]["points"][0]
    assert point["selected_unreviewed"] == 3
    assert point["reviewed_risk"] is None
    assert (point["risk_lower_bound"], point["risk_upper_bound"]) == (0, 1)


def test_reviewed_failure_and_unknown_mix_keeps_correct_risk_bounds(experiment):
    plan, observed, labels = experiment
    bad = Judgment.model_validate({**labels[0].model_dump(), "security_failure": True})
    report = aggregate_selection(DATASET, plan, policy(plan), observed, [bad], signals(observed))
    candidate = report["candidate_results"]["baseline"]
    point = candidate["points"][0]
    assert (
        point["reviewed_risk"] == 1
        and point["risk_lower_bound"] == 1 / 3
        and point["risk_upper_bound"] == 1
    )
    assert candidate["security_failures_across_all_reviewed_outputs"] == 1
    assert candidate["points"][-1]["selected_answers"] == 0


def test_errors_missing_and_unscored_never_disappear_into_abstentions(experiment):
    plan, observed, _ = experiment
    observed.pop(0)
    observed[0] = Generation.model_validate(
        {**observed[0].model_dump(), "outcome": "timeout", "response_sha256": None}
    )
    report = aggregate_selection(DATASET, plan, policy(plan), observed, [], [])
    point = report["candidate_results"]["baseline"]["points"][0]
    assert point["planned_attempts"] == 6 and point["answer_coverage"] == 0
    assert point["missing_attempts"] == point["failed_attempts"] == point["unscored_answers"] == 1
    assert point["native_abstentions"] == 3 and point["withheld_answers"] == 0


@pytest.mark.parametrize("fault", ["duplicate", "unknown", "scorer", "abstained", "plan"])
def test_stale_duplicate_or_mismatched_scores_fail_closed(experiment, fault):
    plan, observed, labels = experiment
    scores, selection = signals(observed), policy(plan)
    if fault == "duplicate":
        scores.append(scores[0])
    elif fault == "plan":
        selection = SelectionPlan.model_validate(
            {**selection.model_dump(), "generation_plan_sha256": "f" * 64}
        )
    else:
        change = (
            {"observation_sha256": "f" * 64}
            if fault == "unknown"
            else {"scorer_sha256": "f" * 64}
            if fault == "scorer"
            else {
                "observation_sha256": identity(
                    next(row for row in observed if row.outcome == "abstained")
                )
            }
        )
        scores[0] = SelectionScore.model_validate({**scores[0].model_dump(), **change})
    with pytest.raises(ValueError):
        aggregate_selection(DATASET, plan, selection, observed, labels, scores)


@pytest.mark.parametrize(
    "thresholds", [[0.5, 0.5], [1.0, 0.0], [float("nan")], [float("inf")], [-0.1], [True]]
)
def test_invalid_threshold_grids_are_rejected(experiment, thresholds):
    with pytest.raises(ValueError):
        SelectionPlan.model_validate(
            {**policy(experiment[0]).model_dump(), "thresholds": thresholds}
        )


def test_selection_cli_verifies_source_snapshot_and_refuses_overwrite(experiment, tmp_path):
    plan, _, _ = experiment
    source = tmp_path / "source.json"
    source.write_text(json.dumps(snapshot()))
    plan = Plan.model_validate(
        {**plan.model_dump(), "source_snapshot_sha256": sha(source.read_bytes())}
    )
    inputs = {
        "plan": plan.model_dump_json(),
        "selection-plan": policy(plan).model_dump_json(),
        "observations": "",
        "judgments": "",
        "scores": "",
    }
    output = tmp_path / "report.json"
    command = [
        sys.executable,
        "-m",
        "evals.runner.selective",
        "--source-manifest",
        str(source),
        "--output",
        str(output),
    ]
    for name, content in inputs.items():
        path = tmp_path / name
        path.write_text(content)
        command.extend(["--" + name, str(path)])
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["candidate_results"]["baseline"]["points"][0]["missing_attempts"] == 6
    assert report["promotion_eligible"] is False
    assert subprocess.run(command, capture_output=True, timeout=30).returncode != 0


def test_prompt_only_change_invalidates_frozen_evaluation_source_snapshot(tmp_path):
    root = tmp_path / "project"
    source_tree(root)
    prompt = root / "apps/api/src/evidencedesk/prompts/policy.txt"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("Only answer from cited evidence.")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(snapshot(root)))
    verify(manifest, root)
    prompt.write_text("Changed prompt with the same Python and dependency files.")
    with pytest.raises(ValueError, match="differs"):
        verify(manifest, root)

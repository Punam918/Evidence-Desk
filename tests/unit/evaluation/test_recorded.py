import json

import pytest

from evals.curation.build import ROOT, jsonl
from evals.curation.validate import case_hash, rows
from evals.runner.recorded import aggregate

DATASET = ROOT / "evals/datasets/benchmark-v1-draft"


def observation(case, variant="dense"):
    return {
        "case_id": case["case_id"],
        "case_sha256": case_hash(case),
        "variant": variant,
        "config_sha256": "a" * 64,
        "collection_sha256": "b" * 64,
        "source_snapshot_sha256": "e" * 64,
        "status": "completed",
        "error_code": None,
        "retrieved": [{k: s[k] for k in ("source_id", "start", "end")} for s in case["gold_spans"]],
        "latency_ms": 12.0,
        "cost_micro_usd": 0,
    }


def test_missing_cases_and_failures_stay_in_denominator_and_labels_gate(tmp_path):
    cases = rows(DATASET / "cases.jsonl")
    path = tmp_path / "observations.jsonl"
    successful = observation(cases[0])
    failed = {
        **observation(cases[1]),
        "status": "timeout",
        "error_code": "deadline_exceeded",
        "latency_ms": 1000.0,
    }
    jsonl(path, [successful, failed])
    with pytest.raises(ValueError, match="human-reviewed"):
        aggregate(DATASET, path, resamples=100)
    report = aggregate(DATASET, path, allow_draft_diagnostic=True, resamples=100)
    dense = report["variants"]["dense"]
    assert dense["declared_cases"] == 100 and dense["observed_cases"] == 2
    assert dense["outcomes"] == {"completed": 1, "timeout": 1, "missing": 98}
    assert dense["metrics"]["recall_at_20"] == {"value": 1 / 80, "n": 80}
    assert dense["retrieval_completion"]["value"] == 1 / 100
    assert dense["latency_ms"]["n"] == 2
    assert dense["latency_ms"]["p95"] > 900
    assert report["status"] == "incomplete" and report["scope"] == "synthetic_draft_diagnostic"
    assert report["promotion_eligible"] is False
    assert report == aggregate(DATASET, path, allow_draft_diagnostic=True, resamples=100)
    assert "Snapshots are created" not in json.dumps(report)


@pytest.mark.parametrize(
    "fault",
    [
        "foreign_source",
        "stale_case",
        "duplicate",
        "split",
        "changed_collection",
        "changed_config",
        "changed_snapshot",
        "coordinate",
        "nonfinite",
        "extra_text",
    ],
)
def test_recorded_transport_integrity_and_scope_fail_closed(tmp_path, fault):
    cases = rows(DATASET / "cases.jsonl")
    record = observation(cases[0])
    extra = []
    if fault == "foreign_source":
        record["retrieved"][0]["source_id"] = "OTHER-WORKSPACE"
    elif fault == "stale_case":
        record["case_sha256"] = "0" * 64
    elif fault == "duplicate":
        extra = [record]
    elif fault == "split":
        record = observation(next(c for c in cases if c["split"] == "test"))
    elif fault == "changed_collection":
        extra = [{**observation(cases[1]), "collection_sha256": "c" * 64}]
    elif fault == "changed_config":
        extra = [{**observation(cases[1]), "config_sha256": "c" * 64}]
    elif fault == "changed_snapshot":
        extra = [{**observation(cases[1]), "source_snapshot_sha256": "c" * 64}]
    elif fault == "coordinate":
        record["retrieved"][0]["end"] = 999999
    elif fault == "nonfinite":
        record["latency_ms"] = float("inf")
    else:
        record["raw_prompt"] = "Private text"
    path = tmp_path / "observations.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [record, *extra]) + "\n")
    with pytest.raises(ValueError):
        aggregate(DATASET, path, allow_draft_diagnostic=True, resamples=100)

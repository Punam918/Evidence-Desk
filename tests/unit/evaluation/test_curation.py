"""Synthetic review identities below are temporary test data, never real label evidence."""

import csv
import json
import shutil

import pytest

from evals.curation.__main__ import export_review
from evals.curation.build import ROOT, build, canonical, sha
from evals.curation.review import import_reviews
from evals.curation.validate import case_hash, double_review_sample, review_gate, rows, validate

DATASET = ROOT / "evals/datasets/benchmark-v1-draft"


@pytest.fixture
def draft(tmp_path):
    path = tmp_path / "draft"
    shutil.copytree(DATASET, path)
    return path


def test_draft_integrity_counts_and_promotion_refusal():
    report = validate(DATASET)
    assert report["counts"] == {
        "questions": 400,
        "security": 200,
        "workflows": 50,
        "documents": 120,
    }
    assert report["split_family_overlap"] == report["normalized_question_overlap"] == 0
    assert report["human_review"]["primary_accepted"] == 0
    assert not report["promotion_eligible"]
    assert (
        report["cluster_count"] == 8
    )  # Never treat400 templated questions as400 independent families.


def test_build_is_deterministic_and_never_overwrites_reviews(tmp_path):
    first = build(tmp_path / "first")
    second = build(tmp_path / "second")
    assert first == second
    assert (tmp_path / "first/cases.jsonl").read_bytes() == (DATASET / "cases.jsonl").read_bytes()
    with pytest.raises(ValueError, match="Destination exists"):
        build(tmp_path / "first")


@pytest.mark.parametrize(
    "change", ["source_bytes", "span", "scope", "split", "missing_manifest_hash", "path_escape"]
)
def test_dataset_tampering_fails_closed(draft, change):
    manifest = json.loads((draft / "manifest.json").read_text())
    cases = rows(draft / "cases.jsonl")
    if change == "source_bytes":
        (draft / manifest["documents"][0]["path"]).write_text("Changed source")
    elif change == "span":
        cases[0]["gold_spans"][0]["start"] += 1
    elif change == "scope":
        cases[0]["scope"]["workspace"] = "B"
    elif change == "split":
        # Preserve strata while splitting an archetype: family detection must still reject it.
        other = next(c for c in cases if c["split"] == "test" and c["category"] == "direct")
        cases[0]["split"], other["split"] = other["split"], cases[0]["split"]
    elif change == "missing_manifest_hash":
        del manifest["artifacts"]["cases.jsonl"]
    else:
        manifest["documents"][0]["path"] = "../outside.txt"
    if change in ("span", "scope", "split"):
        (draft / "cases.jsonl").write_text("".join(canonical(c) + "\n" for c in cases))
        manifest["artifacts"]["cases.jsonl"] = sha((draft / "cases.jsonl").read_bytes())
    (draft / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        validate(draft)


def review(case, reviewer="test-primary", decision="accept"):
    return {
        "case_id": case["case_id"],
        "case_sha256": case_hash(case),
        "reviewer_id": reviewer,
        "decision": decision,
        "human_attestation": True,
        "rubric_version": "human-review-v1",
        "reviewed_at": "2026-10-12T12:00:00+05:45",
        "notes": "Synthetic unit-test record; not a real review.",
    }


def test_review_gate_requires_real_records_and_distinct_second_sample():
    cases = rows(DATASET / "cases.jsonl")
    records = [review(c) for c in cases]
    gate = review_gate(cases, records)
    assert gate["primary_accepted"] == 400 and not gate["ready"]
    assert len(double_review_sample(cases)) == 80
    assert review_gate(cases, records, single_reviewer=True)["ready"]
    records.extend(
        review(c, "test-independent") for c in cases if c["case_id"] in double_review_sample(cases)
    )
    assert review_gate(cases, records)["ready"]
    records.append(review(cases[0], "test-third", decision="revise"))
    assert not review_gate(cases, records)["ready"]


@pytest.mark.parametrize(
    "change",
    [
        "stale_hash",
        "duplicate",
        "no_attestation",
        "invalid_date",
        "no_timezone",
        "blank_reviewer",
        "no_notes",
    ],
)
def test_invalid_review_cannot_promote(change):
    cases = rows(DATASET / "cases.jsonl")
    value = review(cases[0])
    if change == "stale_hash":
        value["case_sha256"] = "0" * 64
    elif change == "no_attestation":
        value["human_attestation"] = False
    elif change == "invalid_date":
        value["reviewed_at"] = "yesterday"
    elif change == "no_timezone":
        value["reviewed_at"] = "2026-10-12T12:00:00"
    elif change == "blank_reviewer":
        value["reviewer_id"] = ""
    elif change == "no_notes":
        value["notes"] = ""
    with pytest.raises(ValueError):
        review_gate(cases, [value, value] if change == "duplicate" else [value])


def test_blinded_export_and_atomic_import_do_not_accept_blank_rows(draft, tmp_path):
    out = tmp_path / "review"
    export_review(draft, out)
    assert len(json.loads((out / "second-review-sample.json").read_text())) == 80
    assert "AI-authored drafts" in (out / "review.html").read_text()
    assert import_reviews(draft, out / "review-sheet.csv") == 0
    with (out / "review-sheet.csv").open() as stream:
        sheet = list(csv.DictReader(stream))
    case = rows(draft / "cases.jsonl")[0]
    sheet[0] = {**review(case), "human_attestation": "true"}
    with (out / "review-sheet.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(sheet[0]))
        writer.writeheader()
        writer.writerows(sheet)
    assert import_reviews(draft, out / "review-sheet.csv") == 1
    assert import_reviews(draft, out / "review-sheet.csv") == 0
    assert validate(draft)["human_review"]["primary_accepted"] == 1
    assert not validate(draft)["promotion_eligible"]

import json
from pathlib import Path

import pytest

from evals.curation.build import jsonl, sha
from evals.curation.validate import rows
from evals.runner.export import imports
from evals.runner.provenance import DIRECTORIES, FILES, snapshot, verify
from evals.runner.recorded import aggregate
from tests.unit.evaluation.test_recorded import DATASET, observation


def test_export_is_contract_valid_incomplete_and_hash_bound(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(snapshot(), sort_keys=True, indent=2) + "\n")
    path = tmp_path / "observations.jsonl"
    record = observation(rows(DATASET / "cases.jsonl")[0])
    record["source_snapshot_sha256"] = sha(source.read_bytes())
    jsonl(path, [record])
    report = aggregate(DATASET, path, allow_draft_diagnostic=True, resamples=100)
    artifact = json.dumps(report).encode()
    values = imports(report, artifact, source, "Scripted test fixture CPU")
    assert set(values) == {"dense"}  # Never invent the configurations of missing variants.
    value = values["dense"]
    assert value.status == "failed" and value.evidence_scope == "draft_diagnostic"
    assert value.case_count == 100 and value.artifact_sha256 == sha(artifact)
    metrics = {m.name: m for m in value.metrics}
    assert metrics["recall_at_20"].n == 80 and metrics["cases_missing"].value == 99
    assert metrics["ndcg_at_10"].value is None and metrics["ndcg_at_10"].n == 0
    assert metrics["retrieval_completion"].ci_low is not None
    with pytest.raises(ValueError, match="Artifact"):
        imports(report, b"{}", source, "CPU")
    report["source_snapshot_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="Observation source"):
        imports(report, json.dumps(report).encode(), source, "CPU")


def source_tree(root: Path):
    for directory in DIRECTORIES:
        (root / directory).mkdir(parents=True, exist_ok=True)
    for file in FILES:
        (root / file).parent.mkdir(parents=True, exist_ok=True)
        (root / file).write_text("source fixture")
    (root / "apps/api/src/code.py").write_text("source = 1")


def test_snapshot_determinism_allowlist_and_staleness(tmp_path):
    root = tmp_path / "project"
    source_tree(root)
    (root / ".env").write_text("DO_NOT_READ=private-canary")
    (root / "apps/api/src/.private.json").write_text("private-canary")
    manifest = snapshot(root)
    assert manifest == snapshot(root) and manifest["git_sha"] is None
    assert ".env" not in manifest["files"] and "private-canary" not in json.dumps(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    assert verify(path, root) == (sha(path.read_bytes()), None)
    (root / "apps/api/src/code.py").write_text("source = 2")
    with pytest.raises(ValueError, match="differs"):
        verify(path, root)


def test_snapshot_rejects_symlink_sources(tmp_path):
    source_tree(tmp_path)
    (tmp_path / "apps/api/src/secret.py").symlink_to(tmp_path / "uv.lock")
    with pytest.raises(ValueError, match="Symbolic"):
        snapshot(tmp_path)

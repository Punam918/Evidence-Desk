"""Translate recorded retrieval aggregates into the public, content-free report contract."""

from pathlib import Path
from typing import Any

from evidencedesk.quality.evaluations import EvaluationInput, Metric

from evals.curation.build import sha
from evals.curation.validate import read_json
from evals.runner.provenance import verify
from evals.runner.recorded import VARIANTS


def imports(
    report: dict[str, Any], artifact: bytes, source_manifest: Path, hardware: str
) -> dict[str, EvaluationInput]:
    snapshot_hash, git_sha = verify(source_manifest)
    if report["source_snapshot_sha256"] != snapshot_hash:
        raise ValueError("Observation source identity differs from the supplied snapshot")
    if read_json(artifact.decode("utf-8")) != report:
        raise ValueError("Artifact bytes do not describe this report")
    output: dict[str, EvaluationInput] = {}
    for variant in VARIANTS:
        data = report["variants"][variant]
        # An absent variant has no declared configuration. Do not fabricate its identity.
        if data["config_sha256"] is None:
            continue
        metrics = [
            Metric(name=name, value=m["value"], n=m["n"], ci_low=None, ci_high=None)
            for name, m in data["metrics"].items()
        ]
        completion = data["retrieval_completion"]
        metrics.append(
            Metric(
                name="retrieval_completion",
                value=completion["value"],
                n=completion["n"],
                ci_low=completion["ci_low"],
                ci_high=completion["ci_high"],
            )
        )
        for name in ("p50", "p95", "p99"):
            metrics.append(
                Metric(
                    name="latency_ms_" + name,
                    value=data["latency_ms"][name],
                    n=data["latency_ms"]["n"],
                    ci_low=None,
                    ci_high=None,
                )
            )
        observed = data["observed_cases"]
        metrics.append(
            Metric(
                name="recorded_cost_micro_usd",
                value=float(data["recorded_cost_micro_usd"]) if observed else None,
                n=observed,
                ci_low=None,
                ci_high=None,
            )
        )
        for status in ("completed", "error", "timeout", "cancelled", "missing"):
            metrics.append(
                Metric(
                    name="cases_" + status,
                    value=float(data["outcomes"].get(status, 0)),
                    n=data["declared_cases"],
                    ci_low=None,
                    ci_high=None,
                )
            )
        for name, measured in report["paired_delta_vs_dense"].get(variant, {}).items():
            metrics.append(
                Metric(
                    name=name + "_delta_vs_dense",
                    value=measured["value"],
                    n=measured["n"],
                    ci_low=measured["ci_low"],
                    ci_high=measured["ci_high"],
                )
            )
        output[variant] = EvaluationInput(
            dataset_version=report["dataset_version"],
            dataset_sha256=report["dataset_sha256"],
            config_sha256=data["config_sha256"],
            git_sha=git_sha,
            source_snapshot_sha256=snapshot_hash,
            evidence_scope="draft_diagnostic"
            if report["scope"] == "synthetic_draft_diagnostic"
            else "reviewed_benchmark",
            variant=variant,
            model_key="retrieval-only",
            hardware=hardware,
            status="completed"
            if data["outcomes"].get("completed", 0) == data["declared_cases"]
            else "failed",
            case_count=data["declared_cases"],
            metrics=metrics,
            artifact_sha256=sha(artifact),
        )
    return output

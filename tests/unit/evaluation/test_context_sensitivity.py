"""Packing/validation invariants, not model-generated semantic answers."""

import json
import socket

import pytest
from evidencedesk.ai.answer_validation import validate_answer
from evidencedesk.errors import Problem

from evals.runner.context_sensitivity import measure, scenarios
from tests.unit.ai.test_answer_validation import outcome
from tests.unit.ai.test_context import packer


async def test_actual_frozen_tokenizers_expose_missing_support_and_keep_variant_denominators(
    monkeypatch,
):
    def denied(*args, **kwargs):
        raise AssertionError("Offline diagnostic attempted network I/O")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    report = await measure()
    assert report["quality_measured"] is False
    assert report["inference_calls"] == report["actual_api_spend_micro_usd"] == 0
    assert len(report["rows"]) == 54
    groups = {}
    for row in report["rows"]:
        groups.setdefault((row["model_key"], row["topic"]), {})[row["variant"]] = row
        assert row["fits_context"] in (True, None)
        assert len(row["packed_hashes"]) <= 8
    assert len(groups) == 6
    for variants in groups.values():
        for variant, label in (
            ("support_first", "E1"),
            ("support_middle", "E4"),
            ("support_last", "E8"),
        ):
            assert variants[variant]["support_evidence_label"] == label
            assert len(variants[variant]["packed_hashes"]) == 8
        # Order changes identity while the candidate source set stays identical.
        assert (
            len(
                {
                    variants[v]["request_sha256"]
                    for v in ("support_first", "support_middle", "support_last")
                }
            )
            == 3
        )
        assert set(variants["support_first"]["packed_hashes"]) == set(
            variants["support_last"]["packed_hashes"]
        )
        assert variants["conflicting"]["support_in_packed_context"]
        assert variants["conflicting"]["conflict_in_packed_context"]
        assert variants["stale_history"]["history_pairs"] == 1
        assert not variants["stale_history"]["conflict_in_packed_context"]
        assert variants["empty"]["packer_abstention"]
        assert variants["empty"]["request_sha256"] is None
        for variant in ("related_but_insufficient", "support_beyond_passage_limit"):
            assert not variants[variant]["support_in_packed_context"]
            assert variants[variant]["request_sha256"] is not None
            assert variants[variant]["packer_abstention"] is None


@pytest.mark.parametrize("topic", ["policy", "technical", "support"])
def test_source_identity_survives_permutation_but_old_labels_cannot_quote_new_passages(topic):
    variants = {s.variant: s for s in scenarios() if s.topic == topic}
    quote = variants["support_only"].ranked.evidence[0].passage.text

    def answer(label):
        return outcome(
            json.dumps(
                {
                    "status": "answered",
                    "sentences": [{"text": f'The source says "{quote}"', "citation_ids": [label]}],
                    "limitations": [],
                }
            )
        )

    for variant, label in (
        ("support_first", "E1"),
        ("support_middle", "E4"),
        ("support_last", "E8"),
    ):
        scenario = variants[variant]
        bundle = packer().pack(scenario.query, scenario.ranked)
        passages = tuple(e.passage for e in bundle.evidence)
        _, citations = validate_answer(answer(label), passages)
        assert citations[0].chunk_id == variants["support_only"].ranked.evidence[0].passage.chunk_id
        if label != "E1":
            with pytest.raises(Problem, match="invalid_model_output"):
                validate_answer(answer("E1"), passages)


def test_history_cannot_supply_a_verbatim_quote_missing_from_current_evidence():
    scenario = next(s for s in scenarios() if s.variant == "stale_history")
    bundle = packer().pack(scenario.query, scenario.ranked, scenario.history)
    raw = json.dumps(
        {
            "status": "answered",
            "sentences": [{"text": f'"{scenario.history[0].assistant}"', "citation_ids": ["E1"]}],
            "limitations": [],
        }
    )
    assert bundle.history_ids
    with pytest.raises(Problem, match="invalid_model_output"):
        validate_answer(outcome(raw), tuple(e.passage for e in bundle.evidence))

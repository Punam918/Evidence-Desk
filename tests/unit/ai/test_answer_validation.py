import json
from dataclasses import replace

import pytest
from evidencedesk.ai.answer_validation import draft_batches, validate_answer
from evidencedesk.chat.models import Answer
from evidencedesk.errors import Problem
from evidencedesk.providers.base import ModelResult

from tests.unit.ai.test_context import ranked


def outcome(raw):
    try:
        answer = Answer.model_validate_json(raw)
    except ValueError:
        answer = None
    return ModelResult("completed", answer, None, "fixture", "", raw)


@pytest.mark.parametrize(
    "raw",
    [
        '{"status":"answered","sentences":[{"text":"x","citation_ids":["E1","E1"]}],"limitations":[]}',
        '{"status":"answered","sentences":[{"text":"x","citation_ids":["E8"]}],"limitations":[]}',
        '{"status":"abstained","sentences":[],"limitations":[],"extra":1}',
        '{"status":"answered","sentences":[{"text":"\\ud800","citation_ids":["E1"]}],"limitations":[]}',
        '{"status":"abstained","status":"abstained","sentences":[],"limitations":["missing"]}',
        "```json\n{}\n```",
    ],
)
def test_invalid_schema_citations_unicode_and_duplicate_properties(raw):
    with pytest.raises(Problem, match="invalid_model_output"):
        validate_answer(outcome(raw), tuple(e.passage for e in ranked(["source"]).evidence))


def test_citations_are_server_owned_and_unicode_drafts_are_byte_bounded():
    raw = json.dumps(
        {
            "status": "answered",
            "sentences": [{"text": "😀 café 東京 " * 100, "citation_ids": ["E1"]}],
            "limitations": [],
        }
    )
    passages = tuple(e.passage for e in ranked(["source"]).evidence)
    answer, citations = validate_answer(outcome(raw), passages)
    assert citations[0].chunk_id == passages[0].chunk_id
    batches = list(draft_batches(answer))
    assert all(len(text.encode()) <= 256 for _, text in batches)
    assert "".join(text for _, text in batches) == answer.sentences[0].text
    bad = replace(outcome(raw), status="incomplete")
    with pytest.raises(Problem):
        validate_answer(bad, passages)


def test_total_output_bound_accounts_for_joined_spaces():
    raw = json.dumps(
        {
            "status": "answered",
            "sentences": [{"text": "x" * 2000, "citation_ids": ["E1"]}] * 10,
            "limitations": [],
        }
    )
    with pytest.raises(Problem):
        validate_answer(outcome(raw), tuple(e.passage for e in ranked(["source"]).evidence))

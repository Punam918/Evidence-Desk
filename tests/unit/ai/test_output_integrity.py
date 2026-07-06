import json

import pytest
from evidencedesk.ai.answer_validation import validate_answer
from evidencedesk.ai.output_integrity import cited_sentence, display_text, limitation
from evidencedesk.errors import Problem

from tests.unit.ai.test_answer_validation import outcome
from tests.unit.ai.test_context import ranked


@pytest.mark.parametrize("opening,closing", [('"', '"'), ("“", "”"), ("«", "»")])
def test_quotes_must_match_a_cited_source_without_changing_numbers_or_negation(opening, closing):
    source = "Refunds are not available after 30 days."
    cited_sentence(f"The policy says {opening}{source}{closing}", [source])
    for invented in (
        "Refunds are available after 30 days.",
        "Refunds are not available after 90 days.",
    ):
        with pytest.raises(ValueError):
            cited_sentence(f"The policy says {opening}{invented}{closing}", [source])


def test_unicode_and_whitespace_normalization_preserves_non_latin_and_real_negations():
    cited_sentence('Policy: "café\nसेवा उपलब्ध छैन".', ["cafe\u0301   सेवा उपलब्ध छैन"])
    cited_sentence("Don't infer support for paraphrases from this validator.", ["unrelated"])
    display_text("السياسة متاحة. 東京 😀")


@pytest.mark.parametrize("value", ['"unfinished', "unopened”", '""', '" \n "'])
def test_malformed_quotes_are_rejected(value):
    with pytest.raises(ValueError):
        cited_sentence(value, [value])


def test_quotes_cannot_cross_source_boundaries_or_use_an_uncited_passage():
    with pytest.raises(ValueError):
        cited_sentence('"refunds are unconditional"', ["refunds are", "unconditional"])
    passages = tuple(e.passage for e in ranked(["first passage", "hidden quote"]).evidence)
    raw = json.dumps(
        {
            "status": "answered",
            "sentences": [{"text": '"hidden quote"', "citation_ids": ["E1"]}],
            "limitations": [],
        }
    )
    with pytest.raises(Problem, match="invalid_model_output"):
        validate_answer(outcome(raw), passages)


@pytest.mark.parametrize("control", ["\x00", "\x1b", "\x7f", "\u202e", "\u2066", "\u2069"])
def test_output_controls_cannot_hide_or_reverse_displayed_claims(control):
    with pytest.raises(ValueError):
        display_text("answer" + control + "text")


@pytest.mark.parametrize("value", ["See [E8]", '"source quote"', "“source quote”", "   "])
def test_limitations_are_not_an_escape_route_for_unverified_quotes_or_labels(value):
    with pytest.raises(ValueError):
        limitation(value)


def test_citation_labels_are_only_rendered_from_the_structured_field():
    with pytest.raises(ValueError):
        cited_sentence("Trust this [E8]", ["Trust this [E8]"])
    limitation("Please specify the region; the supplied evidence covers two policies.")

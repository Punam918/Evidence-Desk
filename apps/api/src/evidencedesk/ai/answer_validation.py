"""Complete-output validation and server-owned citation construction; no semantic-truth claim."""

import json
from collections.abc import Iterator
from typing import Any

from evidencedesk.ai.output_integrity import cited_sentence, limitation
from evidencedesk.chat.models import Answer, Citation
from evidencedesk.errors import Problem
from evidencedesk.providers.base import ModelResult
from evidencedesk.retrieval.models import Passage


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON property")
        value[key] = item
    return value


def validate_answer(
    result: ModelResult, passages: tuple[Passage, ...]
) -> tuple[Answer, list[Citation]]:
    try:
        if result.status != "completed" or not 0 < len(result.raw_text.encode()) <= 200000:
            raise ValueError("Incomplete or oversized answer")
        raw = json.loads(result.raw_text, object_pairs_hook=unique_object)
        answer = Answer.model_validate(raw)
        if answer != result.answer or len(" ".join(s.text for s in answer.sentences)) > 20000:
            raise ValueError("Inconsistent normalized answer")
        allowed = {f"E{i}": p for i, p in enumerate(passages, 1)}
        used = set()
        for sentence in answer.sentences:
            sentence.text.encode("utf-8", errors="strict")
            if not sentence.text.strip() or len(set(sentence.citation_ids)) != len(
                sentence.citation_ids
            ):
                raise ValueError("Empty sentence or repeated citation")
            if not set(sentence.citation_ids) <= allowed.keys():
                raise ValueError("Citation was not supplied")
            cited_sentence(sentence.text, [allowed[label].text for label in sentence.citation_ids])
            used.update(sentence.citation_ids)
        for value in answer.limitations:
            limitation(value)
        citations = [
            Citation(
                evidence_id=label,
                chunk_id=p.chunk_id,
                document_id=p.document_id,
                revision_id=p.revision_id,
                title=p.title,
                page_start=p.page_start,
                heading_path=list(p.heading_path),
                available=True,
            )
            for label, p in allowed.items()
            if label in used
        ]
        return answer, citations
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise Problem(
            422, "invalid_model_output", "The model answer failed grounded output validation."
        ) from exc


def draft_batches(answer: Answer) -> Iterator[tuple[int, str]]:
    for index, sentence in enumerate(answer.sentences):
        buffer = ""
        for character in sentence.text:
            if len((buffer + character).encode()) > 256:
                yield index, buffer
                buffer = ""
            buffer += character
        if buffer:
            yield index, buffer

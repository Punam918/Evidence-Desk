"""Compile the provider subset; enforce conditional answer rules locally afterwards."""

from typing import TYPE_CHECKING, Any

from evidencedesk.chat.models import Answer
from evidencedesk.providers.auxiliary import (
    OutputKind,
    RewritePayload,
    SummaryPayload,
    auxiliary_output,
)
from pydantic import BaseModel

if TYPE_CHECKING:
    from evidencedesk.providers.base import ModelRequest


def answer_format() -> dict[str, Any]:
    return output_format("answer")


def output_format(kind: OutputKind) -> dict[str, Any]:
    # Pydantic validators express the cross-field conditions omitted by the provider subset.
    models: dict[OutputKind, type[BaseModel]] = {
        "answer": Answer,
        "rewrite": RewritePayload,
        "summary": SummaryPayload,
    }
    model = models[kind]
    schema = model.model_json_schema()

    def strip_titles(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: strip_titles(item) for key, item in value.items() if key != "title"}
        if isinstance(value, list):
            return [strip_titles(item) for item in value]
        return value

    return {
        "type": "json_schema",
        "name": "grounded_answer" if kind == "answer" else "memory_" + kind,
        "strict": True,
        "schema": strip_titles(schema),
    }


def request_fingerprint(request: ModelRequest) -> str:
    import hashlib
    from dataclasses import asdict

    from evidencedesk.identity.crypto import canonical_json

    fields = asdict(request)
    if request.output_kind == "answer":
        fields.pop("output_kind")  # Preserve previously frozen grounded-answer request identities.
    return hashlib.sha256(
        canonical_json(
            {"request": fields, "answer_schema": output_format(request.output_kind)}
        ).encode()
    ).hexdigest()


def parse_output(kind: OutputKind, raw: str) -> Answer | None:
    if kind == "answer":
        return Answer.model_validate_json(raw)
    auxiliary_output(kind, raw)
    return None

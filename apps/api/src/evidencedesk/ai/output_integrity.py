"""Deterministic presentation/quotation checks, not semantic entailment or truth detection."""

import re
import unicodedata
from collections.abc import Sequence

INLINE_CITATION = re.compile(r"\[E\d+\]")
# Explicit double quotes are reserved for verbatim source excerpts by prompt v2.
# Apostrophes are not quotation delimiters (contractions and possessives are common).
OPENERS = {'"': '"', "“": "”", "«": "»"}
CLOSERS = {"”", "»"}


def display_text(value: str) -> None:
    value.encode("utf-8", errors="strict")
    if not value.strip() or any(
        (unicodedata.category(char) == "Cc" and char not in "\n\t")
        or unicodedata.bidirectional(char)
        in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
        for char in value
    ):
        raise ValueError("Invalid output display controls")


def normalize_quote(value: str) -> str:
    # Only canonical Unicode and whitespace changes: preserve case, punctuation,
    # quantities, negation and compatibility characters. Never join separate sources.
    return " ".join(unicodedata.normalize("NFC", value).split())


def quotations(value: str) -> tuple[str, ...]:
    found = []
    closing: str | None = None
    start = 0
    for index, char in enumerate(value):
        if closing is not None:
            if char == closing:
                quote = normalize_quote(value[start:index])
                if not quote:
                    raise ValueError("Empty quotation")
                found.append(quote)
                closing = None
        elif char in OPENERS:
            closing, start = OPENERS[char], index + 1
        elif char in CLOSERS:
            raise ValueError("Unbalanced quotation")
    if closing is not None:
        raise ValueError("Unbalanced quotation")
    return tuple(found)


def cited_sentence(value: str, source_texts: Sequence[str]) -> None:
    display_text(value)
    if INLINE_CITATION.search(value):
        raise ValueError("Citation labels belong in the structured citation field")
    quotes = quotations(value)
    if quotes:
        sources = tuple(normalize_quote(source) for source in source_texts)
        if any(not any(quote in source for source in sources) for quote in quotes):
            raise ValueError("Quotation is absent from this sentence's cited evidence")


def limitation(value: str) -> None:
    display_text(value)
    if INLINE_CITATION.search(value) or any(char in value for char in (*OPENERS, *CLOSERS)):
        raise ValueError("Uncited limitations cannot carry source quotations or citation labels")

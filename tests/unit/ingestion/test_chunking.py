from dataclasses import replace
from uuid import uuid4

import pytest
from evidencedesk.chat.query_limits import tokenizer
from evidencedesk.ingestion.blocks import SourceLocator
from evidencedesk.ingestion.chunking import Chunker
from evidencedesk.ingestion.parsers.core import Builder
from evidencedesk.ingestion.profile import IngestionProfile
from hypothesis import given, settings
from hypothesis import strategies as st

TOK = tokenizer(".cache/models", "contracts/config/model-artifacts.lock.json")
CHUNKER = Chunker(TOK)
IDS = [uuid4() for _ in range(4)]


def manifest(values, pages=False):
    builder = Builder("a" * 64, "text/plain")
    for index, value in enumerate(values):
        builder.add(
            value,
            SourceLocator(
                kind="pdf" if pages else "text", index=index, page=index + 1 if pages else None
            ),
        )
    if pages:
        builder.pages = len(values)
    return builder.finish()


@given(
    st.lists(
        st.lists(
            st.sampled_from(["word ", "é ", "中文 ", "😀 ", "fact.\n", "e\u0301 ", "\t"]),
            min_size=1,
            max_size=1000,
        )
        .map("".join)
        .filter(lambda s: s.strip()),
        min_size=1,
        max_size=5,
    )
)
@settings(max_examples=40, deadline=None)
def test_spans_tokens_overlap_and_determinism(values):
    source = manifest(values)
    chunks = CHUNKER.build(source, *IDS)
    assert chunks == CHUNKER.build(source, *IDS)
    stream = "\n\n".join(b.text for b in source.blocks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert stream[chunk.char_start : chunk.char_end] == chunk.text
        assert chunk.token_count == CHUNKER.count(chunk.embedding_text) <= 384
        assert len(chunk.text) <= 12000
    for block in source.blocks:
        covered = block.char_start
        for chunk in chunks:
            if chunk.char_end <= block.char_start or chunk.char_start >= block.char_end:
                continue
            assert chunk.char_start <= covered
            covered = max(covered, min(chunk.char_end, block.char_end))
        assert covered == block.char_end
    for left, right in zip(chunks, chunks[1:], strict=False):
        assert right.char_end > left.char_end
        if right.char_start < left.char_end:
            overlap = stream[right.char_start : left.char_end]
            assert CHUNKER.count(overlap, special=False) <= 48


def test_pages_headings_tables_and_code_preserve_boundaries():
    source = manifest(["Source words. " * 1000, "Other page. " * 1000], pages=True)
    chunks = CHUNKER.build(source, *IDS)
    assert {c.page_start for c in chunks} == {1, 2}
    assert all(c.page_start == c.page_end for c in chunks)
    builder = Builder("a" * 64, "text/markdown")
    builder.heading("Very long heading " * 100, 1)
    builder.add(
        "Line of code\n" * 1000, SourceLocator(kind="markdown", index=0), "code", language="python"
    )
    builder.add(
        "cell words " * 1000,
        SourceLocator(kind="csv", index=1, row=2, column=3),
        "table_cell",
        header="Column three",
    )
    chunks = CHUNKER.build(builder.finish(), *IDS)
    assert all(CHUNKER.count(c.embedding_text) <= 384 for c in chunks)
    assert all(CHUNKER.count(c.embedding_text[: -len(c.text)], special=False) <= 32 for c in chunks)
    assert chunks[-1].source_locator["locators"][0]["column"] == 3
    assert chunks[0].source_locator["language"] == "python"
    assert chunks[0].heading_path == [("Very long heading " * 100).rstrip()]


def test_pathological_unknown_tokens_are_character_bounded_and_tenant_ids_differ():
    source = manifest(["x" * 40000])
    chunks = CHUNKER.build(source, *IDS)
    assert len(chunks) >= 4 and all(len(c.text) <= 12000 for c in chunks)
    assert "".join(c.text for c in chunks) == "x" * 40000
    other = CHUNKER.build(source, uuid4(), *IDS[1:])
    assert not {c.id for c in chunks} & {c.id for c in other}
    changed = Chunker(TOK, replace(IngestionProfile(), target_tokens=256)).build(source, *IDS)
    assert chunks[0].id != changed[0].id


def test_short_evidence_is_not_dropped_and_tokenizer_truncation_is_ignored():
    configured = type(TOK).from_str(TOK.to_str())
    configured.enable_truncation(8)
    chunker = Chunker(configured)
    source = manifest(["A.", "long evidence " * 1000])
    chunks = chunker.build(source, *IDS)
    assert chunks[0].text.startswith("A.") and len(chunks) > 2
    assert all(CHUNKER.count(c.embedding_text) <= 384 for c in chunks)
    with pytest.raises(ValueError):
        Chunker(TOK, replace(IngestionProfile(), max_tokens=512))

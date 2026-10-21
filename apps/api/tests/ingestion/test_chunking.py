"""Chunking: limits, overlap, split points, boundaries and config validation."""

from itertools import pairwise
from typing import Final

import pytest
from pydantic import ValidationError

from vectapilot_api.ingestion.chunking import chunk_document
from vectapilot_api.ingestion.loaders import load_document
from vectapilot_api.ingestion.types import (
    Chunk,
    ChunkingConfig,
    LoadedDocument,
    SourceMeta,
    SourceType,
)

META: Final = SourceMeta(source_id="doc-1", version="v1", language="en")


def markdown(text: str) -> LoadedDocument:
    return load_document(text.encode(), SourceType.MARKDOWN, META)


def check_spans(
    document: LoadedDocument, chunks: tuple[Chunk, ...], config: ChunkingConfig
) -> None:
    for chunk in chunks:
        assert document.text[chunk.char_start : chunk.char_end] == chunk.content
        assert 0 < len(chunk.content) <= config.max_chars
    for before, after in pairwise(chunks):
        assert before.char_start < after.char_start  # always moves forward
        assert after.char_start <= before.char_end + 1  # no gap (one space at most)


def test_huge_single_paragraph() -> None:
    words = " ".join(f"word{i}" for i in range(10_000))  # ~89,000 characters, no newline
    document = markdown(words)
    config = ChunkingConfig()
    chunks = chunk_document(document, config)
    assert len(chunks) > 70
    check_spans(document, chunks, config)
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(document.text)
    assert all(c.content.split()[0].startswith("word") for c in chunks)  # word boundaries
    overlaps = [a.char_end - b.char_start for a, b in pairwise(chunks)]
    assert all(0 < overlap <= config.overlap_chars for overlap in overlaps)
    assert chunk_document(document, config) == chunks  # deterministic


def test_no_whitespace_at_all_is_hard_cut() -> None:
    document = markdown("x" * 2500)
    config = ChunkingConfig(max_chars=1000, overlap_chars=100)
    chunks = chunk_document(document, config)
    assert [(c.char_start, c.char_end) for c in chunks] == [(0, 1000), (900, 1900), (1800, 2500)]


def test_split_prefers_paragraph_then_sentence() -> None:
    config = ChunkingConfig(max_chars=200, overlap_chars=0)
    first, second = "A" * 120 + ".", "B" * 120 + "."
    chunks = chunk_document(markdown(f"{first} tail words\n\n{second}"), config)
    assert chunks[0].content == f"{first} tail words"  # the paragraph break wins
    chunks = chunk_document(markdown(f"{first} {second}"), config)
    assert chunks[0].content == first  # then the sentence end


def test_devanagari_danda_ends_a_sentence() -> None:
    sentence = "दुकान सुबह दस बजे खुलती है।"
    document = load_document(
        " ".join([sentence] * 40).encode(),
        SourceType.MARKDOWN,
        SourceMeta(source_id="doc-hi", version="v1", language="hi"),
    )
    config = ChunkingConfig(max_chars=200, overlap_chars=30)
    chunks = chunk_document(document, config)
    check_spans(document, chunks, config)
    assert all(c.content.endswith("।") for c in chunks)


def test_chunks_never_cross_sections() -> None:
    document = markdown("# One\n" + "alpha " * 100 + "\n# Two\n" + "beta " * 100)
    chunks = chunk_document(document, ChunkingConfig(max_chars=250, overlap_chars=50))
    for chunk in chunks:
        assert ("alpha" in chunk.content) != ("beta" in chunk.content)
        assert chunk.heading_path == (("One",) if "alpha" in chunk.content else ("Two",))


@pytest.mark.parametrize(
    "fields",
    [
        {"max_chars": 1200, "overlap_chars": 600},  # half or more could loop forever
        {"max_chars": 100},
        {"max_chars": 9000},
        {"overlap_chars": -1},
        {"max_chars": 1200, "chunk_tokens": 300},
    ],
)
def test_config_is_validated(fields: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        ChunkingConfig.model_validate(fields)


def test_chunk_span_must_match_content() -> None:
    fields = {"source_id": "a", "version": "v1", "source_type": "csv", "chunk_index": 0}
    Chunk.model_validate({**fields, "content": "abc", "char_start": 4, "char_end": 7})
    with pytest.raises(ValidationError):
        Chunk.model_validate({**fields, "content": "abc", "char_start": 4, "char_end": 8})

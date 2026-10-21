"""Heading-aware, deterministic chunking (M2 PR 3).

Sections (a heading's text, a PDF page, a CSV record) are hard boundaries: a chunk never spans two,
so every chunk has one heading path, one page and one row to cite. Inside a section longer than
`max_chars`, the split point is the last paragraph break in the second half of the window, else the
last line break, else the last sentence end (`.`, `!`, `?` or the Devanagari danda `।`), else the
last whitespace, else a hard cut at `max_chars`. The next chunk then starts up to `overlap_chars`
earlier, at a word start, so a fact on the boundary is whole in at least one chunk.

Same document and config in, same chunks out: no randomness, no clock, no model.
"""

import re
from collections.abc import Iterator
from typing import Final

from vectapilot_api.ingestion.types import Chunk, ChunkingConfig, LoadedDocument

DEFAULT_CHUNKING: Final = ChunkingConfig()

# In priority order; a match's end is where the chunk ends.
_BREAKS: Final = (
    re.compile(r"\n[^\S\n]*\n"),
    re.compile(r"\n"),
    re.compile(r"[.!?\u0964](?=\s)"),
    re.compile(r"\s"),
)
_WHITESPACE: Final = _BREAKS[-1]


def _split_point(text: str, start: int, limit: int, config: ChunkingConfig) -> int:
    lowest = start + config.max_chars // 2
    for pattern in _BREAKS:
        matches = list(pattern.finditer(text, lowest, limit))
        if matches:
            return matches[-1].end()
    return limit


def _spans(text: str, start: int, end: int, config: ChunkingConfig) -> Iterator[tuple[int, int]]:
    position = start
    while True:
        while position < end and text[position].isspace():
            position += 1
        if position >= end:
            return
        if end - position <= config.max_chars:
            cut = end
        else:
            cut = _split_point(text, position, position + config.max_chars, config)
        stop = cut
        while text[stop - 1].isspace():
            stop -= 1
        yield position, stop
        if cut == end:
            return
        next_start = cut - config.overlap_chars
        if config.overlap_chars:
            space = _WHITESPACE.search(text, next_start, cut)
            if space is not None:
                next_start = space.end()
        position = next_start


def chunk_document(
    document: LoadedDocument, config: ChunkingConfig = DEFAULT_CHUNKING
) -> tuple[Chunk, ...]:
    """Split a loaded document into chunks numbered from 0, in document order."""
    chunks: list[Chunk] = []
    for section in document.sections:
        for char_start, char_end in _spans(document.text, section.start, section.end, config):
            chunks.append(
                Chunk(
                    source_id=document.source_id,
                    version=document.version,
                    source_type=document.source_type,
                    chunk_index=len(chunks),
                    content=document.text[char_start:char_end],
                    char_start=char_start,
                    char_end=char_end,
                    page=section.page,
                    row=section.row,
                    heading_path=section.heading_path,
                )
            )
    return tuple(chunks)

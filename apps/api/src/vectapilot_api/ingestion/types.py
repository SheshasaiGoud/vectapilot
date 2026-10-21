"""Typed values of ingestion (M2 PR 3): what a loader returns, how chunking is configured, and the
chunks it returns.

Loaders and the chunker are pure functions: bytes in, values out. No network, no database and no
business is hard-coded; `business_id` is attached when PR 5 writes the rows under row-level security
(ADR 0009), and the chunking config will come from each business's configuration.

Document text is untrusted data (CLAUDE.md rule 5): it is stored and quoted, never executed or
followed. It stays out of `repr()` (`Field(repr=False)`), out of validation errors
(`hide_input_in_errors`) and out of every `IngestionError` message.
"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceType(StrEnum):
    """The file format of a knowledge source (business-context §3.4, supported upload formats)."""

    MARKDOWN = "markdown"
    HTML = "html"
    CSV = "csv"
    PDF = "pdf"


class IngestionModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


# ── Errors: messages carry sizes and limits, never document text ─────────────────────────────


class IngestionError(Exception):
    """Base class. The message is safe to log and to show to the uploader."""


class FileTooLargeError(IngestionError):
    """The file is bigger than `LoadLimits.max_bytes` (checked before any parsing)."""


class TooManyPagesError(IngestionError):
    """The PDF has more pages than `LoadLimits.max_pages` (checked before text extraction)."""


class TextTooLongError(IngestionError):
    """The extracted text is longer than `LoadLimits.max_chars`."""


class EmptyDocumentError(IngestionError):
    """No text to index: an empty file, only whitespace or markup, or a PDF without a text layer."""


class NotUtf8Error(IngestionError):
    """A text format (Markdown, HTML, CSV) that is not valid UTF-8. We reject rather than guess."""


class MalformedDocumentError(IngestionError):
    """The file cannot be parsed as its declared format (or it is an encrypted PDF)."""


# ── Inputs ─────────────────────────────────────────────────────────────────────────────────────


class LoadLimits(IngestionModel):
    """Upper bounds for one untrusted file. Starting values, not measured: revisit with real
    files."""

    max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_pages: int = Field(default=500, gt=0)
    max_chars: int = Field(default=2_000_000, gt=0)


class SourceMeta(IngestionModel):
    """What the business declares about a source (business-context §3.4); the file can't be trusted
    to say it. `title` is optional: without it the loader uses the file's own title, then the id."""

    source_id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    # A BCP 47 tag such as `en`, `hi`, `te` or `en-IN`.
    language: str = Field(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
    title: str | None = Field(default=None, min_length=1, max_length=300)


class ChunkingConfig(IngestionModel):
    """Chunk size in characters (token limits are checked by the embeddings module, PR 4).

    The defaults are starting values, not tuned: they will come from the business config and be
    tuned on the eval dev split only (PR 7), never on the test split.
    """

    max_chars: int = Field(default=1200, ge=200, le=8000)
    overlap_chars: int = Field(default=150, ge=0)

    @model_validator(mode="after")
    def _overlap_below_half(self) -> Self:
        # A split point is never in the first half of the window, so an overlap below half of
        # `max_chars` guarantees that every chunk starts after the previous one (no endless loop).
        if self.overlap_chars * 2 >= self.max_chars:
            raise ValueError("overlap_chars must be less than half of max_chars")
        return self


# ── Outputs ────────────────────────────────────────────────────────────────────────────────────


class Section(IngestionModel):
    """A span of `LoadedDocument.text` that chunks never cross: one heading's text, one PDF page or
    one CSV record. `text[start:end]` is the section's text."""

    heading_path: tuple[str, ...] = ()
    page: int | None = Field(default=None, ge=1)  # PDF only, 1-based
    row: int | None = Field(default=None, ge=1)  # CSV only, 1-based record after the header
    start: int = Field(ge=0)
    end: int


class LoadedDocument(IngestionModel):
    source_id: str
    version: str
    language: str
    title: str
    source_type: SourceType
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # of the uploaded bytes
    page_count: int | None = Field(default=None, ge=1)  # PDF only
    text: str = Field(repr=False)
    sections: tuple[Section, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _sections_in_order(self) -> Self:
        previous_end = 0
        for section in self.sections:
            if not previous_end <= section.start < section.end <= len(self.text):
                raise ValueError("sections must be non-empty, in order and inside the text")
            previous_end = section.end
        return self


class Chunk(IngestionModel):
    """One retrievable piece, with what a citation needs. `content` is exactly
    `LoadedDocument.text[char_start:char_end]`; neighbouring chunks of a section may overlap."""

    source_id: str
    version: str
    source_type: SourceType
    chunk_index: int = Field(ge=0)
    content: str = Field(repr=False, min_length=1)
    char_start: int = Field(ge=0)
    char_end: int
    page: int | None = Field(default=None, ge=1)
    row: int | None = Field(default=None, ge=1)
    heading_path: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _span_matches_content(self) -> Self:
        if self.char_end - self.char_start != len(self.content):
            raise ValueError("char_end - char_start must equal the content length")
        return self

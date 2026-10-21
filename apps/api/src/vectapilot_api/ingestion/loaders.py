"""Loaders: one uploaded file (bytes) to one `LoadedDocument` (M2 PR 3).

Pure functions over bytes: no network, no database, no file system. The input is untrusted
(CLAUDE.md rule 5), so a loader only ever reads text out of it:

- Nothing is executed or fetched. HTML scripts, styles and comments are dropped; attributes
  (`href`, `src`, ...) are never read, so no link is followed. `html.parser` is not an XML parser:
  it has no DTDs and no external entities to resolve. pypdf runs no PDF JavaScript and opens no
  URLs; its own limits cap decompressed stream sizes (`pypdf.Configuration`).
- Text that looks like an instruction ("ignore previous instructions ...") is kept verbatim as
  data. Deciding what text means is the prompt's job (delimited, PR 6), never the loader's.
- Size, page and text limits (`LoadLimits`) raise typed errors before the expensive step.

All text is normalised the same way: UTF-8 (a BOM is dropped), `\\r\\n` and `\\r` to `\\n`, NUL
removed (Postgres `text` rejects it) and Unicode NFC (so the same Hindi word is the same code
points wherever it came from).
"""

import csv
import hashlib
import io
import re
import unicodedata
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Final

from pypdf import PdfReader

from vectapilot_api.ingestion.types import (
    EmptyDocumentError,
    FileTooLargeError,
    LoadedDocument,
    LoadLimits,
    MalformedDocumentError,
    NotUtf8Error,
    Section,
    SourceMeta,
    SourceType,
    TextTooLongError,
    TooManyPagesError,
)

DEFAULT_LIMITS: Final = LoadLimits()

_SECTION_SEPARATOR: Final = "\n\n"


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return unicodedata.normalize("NFC", text)


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        # `from None`: the UnicodeDecodeError holds the whole file in `.object`.
        raise NotUtf8Error(f"not valid UTF-8 (first bad byte at offset {exc.start})") from None
    return _normalize(text)


class _TextBuilder:
    """Joins section texts with a blank line and records each section's offsets."""

    def __init__(self, limits: LoadLimits) -> None:
        self._limits = limits
        self._parts: list[str] = []
        self._length = 0
        self.sections: list[Section] = []

    def add(
        self,
        text: str,
        heading_path: tuple[str, ...] = (),
        page: int | None = None,
        row: int | None = None,
    ) -> None:
        text = text.strip()
        if not text:
            return
        if self._parts:
            self._parts.append(_SECTION_SEPARATOR)
            self._length += len(_SECTION_SEPARATOR)
        start = self._length
        self._parts.append(text)
        self._length += len(text)
        if self._length > self._limits.max_chars:
            raise TextTooLongError(
                f"extracted text exceeds the limit of {self._limits.max_chars} characters"
            )
        self.sections.append(
            Section(heading_path=heading_path, page=page, row=row, start=start, end=self._length)
        )

    def build(
        self,
        data: bytes,
        meta: SourceMeta,
        source_type: SourceType,
        found_title: str | None = None,
        page_count: int | None = None,
        empty_hint: str = "",
    ) -> LoadedDocument:
        if not self.sections:
            raise EmptyDocumentError(f"no text found in the {source_type} file{empty_hint}")
        return LoadedDocument(
            source_id=meta.source_id,
            version=meta.version,
            language=meta.language,
            title=meta.title or found_title or meta.source_id,
            source_type=source_type,
            content_sha256=hashlib.sha256(data).hexdigest(),
            page_count=page_count,
            text="".join(self._parts),
            sections=tuple(self.sections),
        )


def _check_size(data: bytes, limits: LoadLimits) -> None:
    if len(data) > limits.max_bytes:
        raise FileTooLargeError(f"file is {len(data)} bytes; the limit is {limits.max_bytes}")
    if not data.strip():
        raise EmptyDocumentError("the file is empty")


def _push_heading(stack: list[tuple[int, str]], level: int, title: str) -> tuple[str, ...]:
    """Close headings of the same or a deeper level, open this one, return the path."""
    while stack and stack[-1][0] >= level:
        stack.pop()
    stack.append((level, title))
    return tuple(heading for _, heading in stack)


# ── Markdown ───────────────────────────────────────────────────────────────────────────────────

# ATX headings only (`## Title`, optional closing `#`s). Setext headings (underlined with `===`)
# are read as plain text.
_ATX_HEADING: Final = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_FENCE: Final = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def load_markdown(
    data: bytes, meta: SourceMeta, limits: LoadLimits = DEFAULT_LIMITS
) -> LoadedDocument:
    """One section per heading; the heading line starts its section. A heading with no text of its
    own is carried into the next section (no heading-only chunks). `#` lines inside fenced code
    blocks are not headings. The first level-1 heading is the fallback title."""
    _check_size(data, limits)
    builder = _TextBuilder(limits)
    stack: list[tuple[int, str]] = []
    path: tuple[str, ...] = ()
    lines: list[str] = []
    fence: str | None = None
    first_h1: str | None = None
    has_body = False

    for line in _decode(data).split("\n"):
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
        heading = None if fence is not None or fence_match else _ATX_HEADING.match(line)
        if heading and heading.group(2):
            if has_body:
                builder.add("\n".join(lines), path)
                lines, has_body = [], False
            level, title = len(heading.group(1)), heading.group(2).strip()
            path = _push_heading(stack, level, title)
            if level == 1 and first_h1 is None:
                first_h1 = title
        else:
            has_body = has_body or bool(line.strip())
        lines.append(line)
    builder.add("\n".join(lines), path)
    return builder.build(data, meta, SourceType.MARKDOWN, found_title=first_h1)


# ── HTML ───────────────────────────────────────────────────────────────────────────────────────

# `nav` is link lists (boilerplate, no facts); `header` and `footer` stay (addresses, phones).
_DROPPED_TAGS: Final = frozenset({"script", "style", "noscript", "template", "svg", "math", "nav"})
_BLOCK_TAGS: Final = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption",
        "figure", "footer", "form", "header", "hr", "li", "main", "ol", "p", "pre",
        "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }
)  # fmt: skip
_HEADING_TAGS: Final = {f"h{level}": level for level in range(1, 7)}
_WHITESPACE_RUN: Final = re.compile(r"\s+")


def _collapse_lines(text: str) -> str:
    """Collapse whitespace inside each line; drop empty lines."""
    lines = (" ".join(line.split()) for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


class _HtmlTextParser(HTMLParser):
    def __init__(self, builder: _TextBuilder) -> None:
        super().__init__(convert_charrefs=True)
        self._builder = builder
        self._dropped_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._heading_level: int | None = None
        self._heading_parts: list[str] = []
        self._stack: list[tuple[int, str]] = []
        self._path: tuple[str, ...] = ()
        self._parts: list[str] = []
        self._has_body = False
        self.first_h1: str | None = None

    @property
    def title(self) -> str | None:
        return " ".join("".join(self._title_parts).split()) or None

    def _flush(self) -> None:
        self._builder.add(_collapse_lines("".join(self._parts)), self._path)
        self._parts = []
        self._has_body = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROPPED_TAGS:
            self._dropped_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _HEADING_TAGS and self._dropped_depth == 0:
            if self._has_body:  # else carry a heading-only section into the next one
                self._flush()
            self._heading_level = _HEADING_TAGS[tag]
            self._heading_parts = []
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED_TAGS:
            self._dropped_depth = max(0, self._dropped_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif _HEADING_TAGS.get(tag) == self._heading_level and self._heading_level is not None:
            title = " ".join("".join(self._heading_parts).split())
            if title:
                self._path = _push_heading(self._stack, self._heading_level, title)
                if self._heading_level == 1 and self.first_h1 is None:
                    self.first_h1 = title
            self._heading_level = None
            self._parts.append("\n")
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        # Newlines in the source are spaces to a browser; only block tags break lines (`<pre>`
        # is flattened too, which is fine for retrieval).
        data = _WHITESPACE_RUN.sub(" ", data)
        if self._in_title:
            self._title_parts.append(data)
        elif self._dropped_depth == 0:
            self._parts.append(data)
            if self._heading_level is not None:
                self._heading_parts.append(data)
            elif data.strip():
                self._has_body = True

    def close(self) -> None:
        super().close()
        self._flush()


def load_html(data: bytes, meta: SourceMeta, limits: LoadLimits = DEFAULT_LIMITS) -> LoadedDocument:
    """Visible text only, one section per heading (`h1`-`h6`). `<title>` (else the first `h1`) is
    the fallback title. Comments, `<script>`, `<style>`, `<nav>`, `<template>`, `<svg>` are dropped;
    a heading with no text of its own is carried into the next section."""
    _check_size(data, limits)
    builder = _TextBuilder(limits)
    parser = _HtmlTextParser(builder)
    parser.feed(_decode(data))
    parser.close()
    found = parser.title or parser.first_h1
    return builder.build(data, meta, SourceType.HTML, found_title=found)


# ── CSV ────────────────────────────────────────────────────────────────────────────────────────


def load_csv(data: bytes, meta: SourceMeta, limits: LoadLimits = DEFAULT_LIMITS) -> LoadedDocument:
    """Comma-separated, first non-empty row is the header. Each later record becomes one section
    as `header: value` lines, so every chunk names its own columns. Empty cells are left out;
    cells beyond the header are named `column N`. `row` counts records after the header (blank
    records included), so the spreadsheet row is `row + 1`. Formula-like cells stay plain text."""
    _check_size(data, limits)
    builder = _TextBuilder(limits)
    reader = csv.reader(io.StringIO(_decode(data), newline=""))
    header: list[str] | None = None
    row_number = 0
    try:
        for record in reader:
            cells = [cell.strip() for cell in record]
            if header is None:
                if any(cells):
                    header = [cell or f"column {i}" for i, cell in enumerate(cells, start=1)]
                continue
            row_number += 1
            names = header + [f"column {i}" for i in range(len(header) + 1, len(cells) + 1)]
            lines = [f"{name}: {cell}" for name, cell in zip(names, cells, strict=False) if cell]
            builder.add("\n".join(lines), row=row_number)
    except csv.Error:
        raise MalformedDocumentError(f"unreadable CSV near line {reader.line_num}") from None
    return builder.build(data, meta, SourceType.CSV, empty_hint=" (a header and no records?)")


# ── PDF ────────────────────────────────────────────────────────────────────────────────────────


def load_pdf(data: bytes, meta: SourceMeta, limits: LoadLimits = DEFAULT_LIMITS) -> LoadedDocument:
    """Text page by page with 1-based page numbers; each page is a section (chunks never cross a
    page, so a citation names one page). Pages without a text layer are skipped but still
    counted. Encrypted PDFs are refused. No heading detection in PDFs."""
    _check_size(data, limits)
    builder = _TextBuilder(limits)
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise MalformedDocumentError("encrypted PDFs are not supported")
        page_count = len(reader.pages)
        if page_count > limits.max_pages:
            raise TooManyPagesError(f"PDF has {page_count} pages; the limit is {limits.max_pages}")
        for number, page in enumerate(reader.pages, start=1):
            builder.add(_normalize(page.extract_text()), page=number)
        found = reader.metadata.title if reader.metadata else None
    except (MalformedDocumentError, TooManyPagesError, TextTooLongError):
        raise
    except Exception as exc:
        # A parser fed hostile bytes can fail in many ways (pypdf's own errors, but also KeyError,
        # ValueError, RecursionError, ...). Only the exception's type is reported: its message may
        # quote the file. Exception, never BaseException, so cancellation still propagates.
        raise MalformedDocumentError(f"unreadable PDF ({type(exc).__name__})") from None
    title = " ".join(_normalize(found).split()) if isinstance(found, str) else None
    return builder.build(
        data,
        meta,
        SourceType.PDF,
        found_title=title or None,
        page_count=page_count or None,
        empty_hint=" (no text layer: scanned PDFs need OCR, which is M6)",
    )


# ── Dispatch ───────────────────────────────────────────────────────────────────────────────────

LOADERS: Final[dict[SourceType, Callable[[bytes, SourceMeta, LoadLimits], LoadedDocument]]] = {
    SourceType.MARKDOWN: load_markdown,
    SourceType.HTML: load_html,
    SourceType.CSV: load_csv,
    SourceType.PDF: load_pdf,
}


def load_document(
    data: bytes,
    source_type: SourceType,
    meta: SourceMeta,
    limits: LoadLimits = DEFAULT_LIMITS,
) -> LoadedDocument:
    """Load one file whose format the business declared (never guessed from its content)."""
    return LOADERS[source_type](data, meta, limits)

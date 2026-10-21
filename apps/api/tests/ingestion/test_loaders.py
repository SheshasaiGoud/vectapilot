"""Loader edge cases: empty and non-UTF-8 input, limits, malformed files, headings, hostile HTML."""

import ast
import io
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError
from pypdf import PdfWriter

from tests.ingestion.pdf_builder import build_pdf
from vectapilot_api.ingestion import loaders
from vectapilot_api.ingestion.loaders import LOADERS, load_document
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

META: Final = SourceMeta(source_id="doc-1", version="v1", language="en")
TEXT_TYPES: Final = (SourceType.MARKDOWN, SourceType.HTML, SourceType.CSV)


def load(data: bytes, source_type: SourceType, **limits: int) -> LoadedDocument:
    return load_document(data, source_type, META, LoadLimits(**limits))


def test_every_source_type_has_a_loader() -> None:
    assert set(LOADERS) == set(SourceType)


@pytest.mark.parametrize("source_type", list(SourceType))
@pytest.mark.parametrize("data", [b"", b"  \n\t\n"])
def test_empty_file(source_type: SourceType, data: bytes) -> None:
    with pytest.raises(EmptyDocumentError, match="empty"):
        load(data, source_type)


@pytest.mark.parametrize(
    ("data", "source_type"),
    [
        (b"<html><script>alert(1)</script><!-- only a comment --></html>", SourceType.HTML),
        (b"sku,name\n,\n", SourceType.CSV),
        (build_pdf([[], []]), SourceType.PDF),
    ],
)
def test_no_text_is_an_empty_document(data: bytes, source_type: SourceType) -> None:
    with pytest.raises(EmptyDocumentError) as info:
        load(data, source_type)
    if source_type is SourceType.PDF:
        assert "OCR" in str(info.value)


@pytest.mark.parametrize("source_type", TEXT_TYPES)
def test_non_utf8_is_refused_without_quoting_the_file(source_type: SourceType) -> None:
    with pytest.raises(NotUtf8Error) as info:
        load("café menu".encode("latin-1"), source_type)
    assert "offset 3" in str(info.value)
    assert "caf" not in str(info.value)
    assert info.value.__cause__ is None


def test_text_is_normalised() -> None:
    data = "﻿# Title\r\n\r\nCafé \x00open\rdaily".encode()
    assert load(data, SourceType.MARKDOWN).text == "# Title\n\nCafé open\ndaily"


@pytest.mark.parametrize("source_type", list(SourceType))
def test_file_size_is_checked_before_parsing(source_type: SourceType) -> None:
    with pytest.raises(FileTooLargeError, match="15 bytes; the limit is 10"):
        load(b"not a real file", source_type, max_bytes=10)  # even the PDF never reaches pypdf


def test_page_limit() -> None:
    with pytest.raises(TooManyPagesError, match="3 pages; the limit is 2"):
        load(build_pdf([["a"], ["b"], ["c"]]), SourceType.PDF, max_pages=2)


@pytest.mark.parametrize("source_type", list(SourceType))
def test_text_limit(source_type: SourceType) -> None:
    lines = [f"Line {i} of a long policy" for i in range(20)]
    data = {
        SourceType.MARKDOWN: "\n\n".join(lines).encode(),
        SourceType.HTML: "".join(f"<p>{line}</p>" for line in lines).encode(),
        SourceType.CSV: ("text\n" + "\n".join(lines)).encode(),
        SourceType.PDF: build_pdf([[line] for line in lines]),
    }[source_type]
    with pytest.raises(TextTooLongError):
        load(data, source_type, max_chars=100)


@pytest.mark.parametrize(
    "data",
    [b"%PDF-1.4\nthis is not a PDF body\n%%EOF", b"plain text, not a PDF at all"],
)
def test_malformed_pdf(data: bytes) -> None:
    with pytest.raises(MalformedDocumentError, match=r"unreadable PDF \(\w+\)") as info:
        load(data, SourceType.PDF)
    assert "PDF body" not in str(info.value)
    assert info.value.__cause__ is None


def test_encrypted_pdf_is_refused() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("secret", algorithm="RC4-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(MalformedDocumentError, match="encrypted"):
        load(buffer.getvalue(), SourceType.PDF)


def test_malformed_csv() -> None:
    data = b'text\n"' + b"x" * 200_000 + b'"\n'  # above the csv module's field size limit
    with pytest.raises(MalformedDocumentError, match="unreadable CSV near line"):
        load(data, SourceType.CSV)


NESTED_PATHS: Final = [
    ("A",),
    ("A", "B"),
    ("A", "B", "C"),
    ("A", "D"),
    ("E",),
    ("E", "F"),  # a skipped level (# then ####) still nests
]


def test_nested_markdown_headings() -> None:
    data = b"# A\na\n## B\nb\n### C\nc\n## D\nd\n# E\ne\n#### F\nf\n"
    document = load(data, SourceType.MARKDOWN)
    assert [s.heading_path for s in document.sections] == NESTED_PATHS


def test_nested_html_headings() -> None:
    data = b"<h1>A</h1>a<h2>B</h2>b<h3>C</h3>c<h2>D</h2>d<h1>E</h1>e<h4>F</h4>f"
    document = load(data, SourceType.HTML)
    assert [s.heading_path for s in document.sections] == NESTED_PATHS


def test_markdown_edge_lines() -> None:
    # `#` alone is text; a fence closes only with its own marker; an unclosed fence runs to the end.
    data = b"Intro\n#\n~~~\n# no\n```\n# still no\n~~~\n# Real\nbody\n```\n# open fence"
    document = load(data, SourceType.MARKDOWN)
    assert [s.heading_path for s in document.sections] == [(), ("Real",)]
    assert document.title == "Real"


def test_html_entities_are_not_resolved_from_a_dtd() -> None:
    data = (
        b'<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b"<p>Tom &amp; Jerry &xxe; &lt;b&gt;</p>"
    )
    text = load(data, SourceType.HTML).text
    assert "Tom & Jerry &xxe; <b>" in text  # standard entities decoded, the custom one is not
    assert "root:" not in text


def test_title_fallbacks() -> None:
    named = SourceMeta(source_id="doc-1", version="v1", language="hi", title="Declared")
    assert load_document(b"# From file\nx", SourceType.MARKDOWN, named).title == "Declared"
    assert load(b"<h1>First  heading</h1>x", SourceType.HTML).title == "First heading"
    assert load(b"no heading", SourceType.MARKDOWN).title == "doc-1"
    assert load(build_pdf([["x"]]), SourceType.PDF).title == "doc-1"


@pytest.mark.parametrize(
    "fields",
    [
        {"source_id": "", "version": "v1", "language": "en"},
        {"source_id": "a", "version": "v1", "language": "English"},
        {"source_id": "a", "version": "v1", "language": "en", "business_id": "x"},
    ],
)
def test_source_meta_is_validated(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        SourceMeta.model_validate(fields)


def test_sections_must_fit_the_text() -> None:
    base = {
        "source_id": "a",
        "version": "v1",
        "language": "en",
        "title": "t",
        "source_type": SourceType.MARKDOWN,
        "content_sha256": "0" * 64,
        "text": "abcdef",
    }
    LoadedDocument.model_validate({**base, "sections": [Section(start=0, end=6)]})
    for bad in ([Section(start=0, end=4), Section(start=3, end=6)], [Section(start=0, end=7)]):
        with pytest.raises(ValidationError):
            LoadedDocument.model_validate({**base, "sections": bad})


def test_repr_hides_document_text() -> None:
    document = load(b"# T\nconfidential price list", SourceType.MARKDOWN)
    assert "confidential" not in repr(document)


ALLOWED_IMPORTS: Final = {
    "collections",
    "csv",
    "enum",
    "hashlib",
    "html",
    "io",
    "pydantic",
    "pypdf",
    "re",
    "typing",
    "unicodedata",
    "vectapilot_api",
}


def test_ingestion_imports_nothing_that_could_fetch_or_write() -> None:
    """No network, database, subprocess or file-system module; extend the list deliberately."""
    for path in Path(loaders.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name.split(".")[0] in ALLOWED_IMPORTS, f"{path.name} imports {name}"

"""M6 PR 2: the fixture writer's new options (an image XObject, `3 Tr`, an x-offset and a row per
line) leave every committed PDF byte-identical, and pypdf reads them as the plan's risks say."""

import hashlib
import io
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pypdf import PdfReader

from tests.ingestion.pdf_builder import Line, Page, build_pdf
from tests.tools.helpers import CORPUS_DIR

API_DIR: Final = Path(__file__).resolve().parents[2]
COMMITTED_PDFS: Final = {
    CORPUS_DIR / "sunrise-clinic" / "fees-2026-04.pdf": (
        "f2c8c9524cc15bed2025b6f9d154cf21b2725a756f4b2f7ed567d7b6f2fbcf61"
    ),
    API_DIR / "tests" / "ingestion" / "fixtures" / "policy.pdf": (
        "aab2d2445b5c200f0f88458d8ebbe647ee69cd8abf8ad759cfb0b676be0fe7c7"
    ),
}
"""Each committed PDF's SHA-256 as committed before PR 2 (the rebuild tests compare bytes)."""


def text_of(data: bytes) -> list[str]:
    return [page.extract_text() for page in PdfReader(io.BytesIO(data)).pages]


def test_the_committed_pdfs_are_unchanged() -> None:
    for path, sha256 in COMMITTED_PDFS.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha256, path.name


def test_plain_pages_are_written_exactly_as_before() -> None:
    # The original writer's output for one page, byte for byte (kept here as the reference).
    content = b"BT /F1 12 Tf 14 TL 72 720 Td (a) Tj T* (b \\(c\\)) Tj ET"
    built = build_pdf([["a", "b (c)"], []], title="T")

    assert b"stream\n" + content + b"\nendstream" in built
    assert b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>" in built
    assert b"/XObject" not in built and b" Tr" not in built and b" Tm" not in built
    assert b"8 0 obj\n<< /Title (T) >>" in built  # pages 4-7, then the info object


def test_an_image_page_has_one_raw_image_xobject_after_the_info_object() -> None:
    built = build_pdf([Page(["Name: A"], image=True), ["B"]], title="T")
    reader = PdfReader(io.BytesIO(built))

    image = reader.pages[0]["/Resources"]["/XObject"]["/Im1"].get_object()  # type: ignore[index]
    assert (image["/Subtype"], image["/Width"], image["/Height"]) == ("/Image", 1, 1)
    assert "/XObject" not in reader.pages[1]["/Resources"]  # type: ignore[operator]
    assert b"9 0 obj\n<< /Type /XObject /Subtype /Image" in built  # after the info object (8)
    assert text_of(built) == ["Name: A", "B"]


def test_a_hidden_line_is_in_the_text_layer() -> None:
    built = build_pdf([[Line("Name: Shown"), Line("Name: Hidden", hidden=True), "After"]])

    assert b"3 Tr" in built and b"0 Tr" in built
    assert text_of(built) == ["Name: Shown\nName: Hidden\nAfter"]


def test_a_table_written_by_columns_reads_as_a_label_column_then_a_value_column() -> None:
    # The plan's risk ("Clean inputs and layouts"): such fields should end in review.
    by_columns = [Line("Name", row=0), Line("Born", row=1)]
    by_columns += [Line("Asha", x=200, row=0), Line("01/02/1990", x=200, row=1)]
    by_rows = [Line("Name", row=0), Line("Asha", x=200, row=0)]
    by_rows += [Line("Born", row=1), Line("01/02/1990", x=200, row=1)]

    assert text_of(build_pdf([by_columns, by_rows])) == [
        "Name\nBorn\nAsha\n01/02/1990",
        "Name Asha\nBorn 01/02/1990",
    ]


def test_the_options_are_deterministic() -> None:
    pages: list[Sequence[str | Line] | Page] = [
        Page([Line("x", x=10, hidden=True)], image=True),
        [Line("y", row=3)],
        [],
    ]
    assert build_pdf(pages, title="T") == build_pdf(pages, title="T")

"""A minimal, hand-written PDF writer for test fixtures (no extra tool or package).

Same input, same bytes: fixed object order, no timestamps, no random file id. One standard font
(Helvetica, which every PDF reader has built in), ASCII text, one line per string.

**Options for M6's extraction fixtures** (plan PR 2, "Fixture writer"). A page may be a `Page`
instead of a list of strings, and a line a `Line` instead of a string:
- `Line(x=...)` moves a line right of the margin and `Line(row=...)` puts it on a given text row,
  so a two-column table can be written as a label column, then a value column (as some real
  writers do, and as pypdf then reads it back);
- `Line(hidden=True)` draws the line in text render mode `3 Tr` (invisible, still in the text
  layer: ADR 0017's known gap, "hidden text layers");
- `Page(image=True)` draws one tiny raw image XObject (a 1x1 grey pixel) on the page, as a
  letterhead logo or a scanned attachment would.

**A page of plain strings without an image is written exactly as before**, so every committed
corpus PDF stays byte-identical (`tests/extraction/test_pdf_builder.py`, and the rebuild tests of
`tests/evalset/test_sample_corpus.py` and `tests/ingestion/test_golden.py`). The options change
only the pages that use them.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

LEFT: Final = 72
"""The left margin, in points."""
TOP: Final = 720
"""The first text row's baseline, in points from the bottom."""
LEADING: Final = 14
"""Points between two text rows (`14 TL`)."""
IMAGE_PLACEMENT: Final = b"q 48 0 0 48 480 724 cm /Im1 Do Q "
"""Where the image is drawn: 48 points square, top right (a logo's place)."""
IMAGE_OBJECT: Final = (
    b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray"
    b" /BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream"
)
"""One raw (unfiltered) 1x1 8-bit grey pixel: the smallest image XObject a reader accepts."""


@dataclass(frozen=True)
class Line:
    """One line of text with options (module docstring)."""

    text: str
    x: int = 0
    """Points right of the left margin."""
    row: int | None = None
    """The text row from the top (0 is the first); None is the row after the previous line's."""
    hidden: bool = False
    """Text render mode `3 Tr`: in the text layer, never painted."""


@dataclass(frozen=True)
class Page:
    """One page with options (module docstring)."""

    lines: Sequence[str | Line] = field(default=())
    image: bool = False


def _pdf_string(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return b"(" + escaped.encode("ascii") + b")"


def _plain(page: Sequence[str | Line] | Page) -> Sequence[str] | None:
    """The page's lines if it is written the original way (strings only, no image), else None."""
    if isinstance(page, Page):
        return None
    if all(isinstance(line, str) for line in page):
        return [line for line in page if isinstance(line, str)]
    return None


def _positioned(lines: Sequence[str | Line]) -> bytes:
    """A text object that places every line with its own text matrix (`Tm`)."""
    parts: list[bytes] = []
    row = -1
    mode = 0
    for item in lines:
        line = Line(item) if isinstance(item, str) else item
        row = row + 1 if line.row is None else line.row
        wanted = 3 if line.hidden else 0
        if wanted != mode:
            parts.append(b"%d Tr" % wanted)
            mode = wanted
        y = TOP - LEADING * row
        parts.append(b"1 0 0 1 %d %d Tm " % (LEFT + line.x, y) + _pdf_string(line.text) + b" Tj")
    return b"BT /F1 12 Tf " + b" ".join(parts) + b" ET" if parts else b""


def _content(page: Sequence[str | Line] | Page) -> bytes:
    plain = _plain(page)
    if plain is not None:
        shown = b" T* ".join(_pdf_string(line) + b" Tj" for line in plain)
        return b"BT /F1 12 Tf 14 TL 72 720 Td " + shown + b" ET" if plain else b""
    lines = page.lines if isinstance(page, Page) else page
    image = isinstance(page, Page) and page.image
    return (IMAGE_PLACEMENT if image else b"") + _positioned(lines)


def build_pdf(pages: Sequence[Sequence[str | Line] | Page], title: str | None = None) -> bytes:
    """A PDF with one page per item; each page shows its lines top to bottom (no lines = blank).
    A `Page` or a `Line` adds the options in the module docstring."""
    first_page = 4
    info_number = first_page + 2 * len(pages)
    image_number = info_number + (1 if title is not None else 0)
    any_image = any(isinstance(page, Page) and page.image for page in pages)
    kids = b" ".join(b"%d 0 R" % (first_page + 2 * i) for i in range(len(pages)))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(pages),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for i, page in enumerate(pages):
        image = (
            b" /XObject << /Im1 %d 0 R >>" % image_number
            if isinstance(page, Page) and page.image
            else b""
        )
        page_obj = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >>" + image + b" >> /Contents %d 0 R >>"
        ) % (first_page + 2 * i + 1)
        content = _content(page)
        stream = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        objects += [page_obj, stream]
    if title is not None:
        objects.append(b"<< /Title " + _pdf_string(title) + b" >>")
    if any_image:
        objects.append(IMAGE_OBJECT)

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    info = b" /Info %d 0 R" % info_number if title is not None else b""
    out += b"trailer\n<< /Size %d /Root 1 0 R%s >>\n" % (len(objects) + 1, info)
    out += b"startxref\n%d\n%%%%EOF\n" % xref_at
    return bytes(out)

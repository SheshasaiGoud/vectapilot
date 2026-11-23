"""The PDF of a red-team document case (M7 PR 7; category D): built at run time from the case's
`fixture.document`, never a committed file (`cases.DocumentFixture`).

The same small writer as the tests' `tests/ingestion/pdf_builder.py` (which `src/` cannot import):
fixed object order, no timestamps, no file id, so the same case always gives the same bytes. One
standard font (Helvetica, WinAnsi), one line per text row, every line placed with its own text
matrix (`Tm`) on a US Letter page (612 x 792 points). Each page draws its visible lines first, in
black, then its hidden lines, each as its `HiddenLine.mode` says:
- `invisible_render`: text render mode `3 Tr` (in the text layer, never painted);
- `white_fill`: filled in white (`1 1 1 rg`), white on the white page;
- `off_page`: started right of the page's box (`OFF_PAGE_X`), on the next text row.

A line is written in WinAnsi (cp1252): a character outside it raises `ValueError` (the case
cannot be built, so it never runs half-built). Every text is a case's, filled in memory only.
"""

from collections.abc import Callable, Sequence
from typing import Final

from vectapilot_api.redteam.cases import DocumentFixture, HiddenLine

LEFT: Final = 72
"""The left margin, in points."""
TOP: Final = 720
"""The first text row's baseline, in points from the bottom."""
LEADING: Final = 14
"""Points between two text rows."""
OFF_PAGE_X: Final = 900
"""Where an `off_page` line starts: right of the page's 612-point width."""

_MODE_OPERATORS: Final = {
    "invisible_render": (b"3 Tr", b"0 Tr"),
    "white_fill": (b"1 1 1 rg", b"0 g"),
}
"""A hidden mode's operator before its line, and the one that restores the visible state."""


def _pdf_string(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return b"(" + escaped.encode("cp1252") + b")"


def _shown(text: str, x: int, row: int) -> bytes:
    return b"1 0 0 1 %d %d Tm " % (x, TOP - LEADING * row) + _pdf_string(text) + b" Tj"


def _hidden(line: HiddenLine, text: str, row: int) -> bytes:
    if line.mode == "off_page":
        return _shown(text, OFF_PAGE_X, row)
    before, after = _MODE_OPERATORS[line.mode]
    return before + b" " + _shown(text, LEFT, row) + b" " + after


def page_content(
    visible: Sequence[str], hidden: Sequence[HiddenLine], fill: Callable[[str], str]
) -> bytes:
    """One page's content stream (module docstring)."""
    parts = [_shown(fill(text), LEFT, row) for row, text in enumerate(visible)]
    parts += [_hidden(line, fill(line.text), len(visible) + row) for row, line in enumerate(hidden)]
    return b"BT /F1 12 Tf " + b" ".join(parts) + b" ET"


def document_pdf(document: DocumentFixture, fill: Callable[[str], str]) -> bytes:
    """The case's document as PDF bytes; `fill` fills each line's placeholders
    (`FilledCase.text`)."""
    first_page = 4
    pages = document.pages
    kids = b" ".join(b"%d 0 R" % (first_page + 2 * i) for i in range(len(pages)))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(pages),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for i, page in enumerate(pages):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
            b" /Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
            % (first_page + 2 * i + 1)
        )
        content = page_content(page.visible, page.hidden, fill)
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref_at
    return bytes(out)

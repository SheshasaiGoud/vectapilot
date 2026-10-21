"""A minimal, hand-written PDF writer for test fixtures (no extra tool or package).

Same input, same bytes: fixed object order, no timestamps, no random file id. One standard font
(Helvetica, which every PDF reader has built in), ASCII text, one line per string.
"""

from collections.abc import Sequence


def _pdf_string(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return b"(" + escaped.encode("ascii") + b")"


def build_pdf(pages: Sequence[Sequence[str]], title: str | None = None) -> bytes:
    """A PDF with one page per item; each page shows its lines top to bottom (no lines = blank)."""
    first_page = 4
    info_number = first_page + 2 * len(pages)
    kids = b" ".join(b"%d 0 R" % (first_page + 2 * i) for i in range(len(pages)))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(pages),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for i, lines in enumerate(pages):
        page_obj = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (first_page + 2 * i + 1)
        )
        shown = b" T* ".join(_pdf_string(line) + b" Tj" for line in lines)
        content = b"BT /F1 12 Tf 14 TL 72 720 Td " + shown + b" ET" if lines else b""
        stream = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        objects += [page_obj, stream]
    if title is not None:
        objects.append(b"<< /Title " + _pdf_string(title) + b" >>")

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

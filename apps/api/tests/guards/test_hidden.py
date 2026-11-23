"""M7 PR 7: hidden text from pypdf's content-stream operators (`guards.hidden`; G3, G7). Unit.

Every PDF is written here, by hand, from a raw content stream (as tests/ingestion/pdf_builder.py
writes the corpus): synthetic text, ASCII, one standard font. Each test names the operators it
uses, so a reader can see why a run counts as hidden or not.

- **Hidden:** render modes 3 and 7 (and any mode outside 0-7); white or near-white fill in gray,
  RGB, CMYK, a device space chosen with `cs`, an ICC-based or calibrated space by its components
  (an ICC-based space with four starts white), in every mode that fills; a white stroke on
  stroked text; a fill or stroke alpha near 0 from an ExtGState; a start point outside the crop
  box (by `Td`, `Tm`, `T*`, `cm`, `'`, a form's `/Matrix` before the CTM, or a crop box smaller
  than the media box); hidden text inside a Form XObject; a form nested too deep (in doubt).
- **Not hidden:** ordinary text, grey that is not near white, a pattern colour, a fill colour on
  stroked text, a state `Q` restored, whitespace and empty runs, and text that rotations, scaling
  and line moves land on the page (the matrices compose in PDF's order).
- **Bounded (G3):** past `OPS_MAX` operations the walk raises `OverCap` (exactly there), also
  through a form drawn many times, which is parsed once per page; a form that draws itself stops
  at the depth bound.
- **Known gaps, pinned as not detected:** a tiny font, `0 Tz`, a clipping path, a white overlay,
  a `TJ` displacement, and optional content that is off by default.
"""

import io
from collections.abc import Sequence
from typing import Final

import pytest
from pypdf import PdfReader
from pypdf.generic import ContentStream

from tests.extraction.standins import pdf
from tests.ingestion.pdf_builder import Line, Page
from vectapilot_api.extraction.text import FORM_DEPTH_MAX, read_pages
from vectapilot_api.guards import document, hidden
from vectapilot_api.guards.caps import OverCap
from vectapilot_api.guards.document import read_for_screen
from vectapilot_api.guards.hidden import hidden_runs

FONT: Final = b"/Font << /F1 3 0 R >>"
VISIBLE: Final = b"BT /F1 12 Tf 72 720 Td (Visible line) Tj ET "
"""An ordinary line every page starts with: never counted."""


def stream(dictionary: bytes, content: bytes) -> bytes:
    return (
        b"<< "
        + dictionary
        + b" /Length %d >>\nstream\n" % len(content)
        + content
        + (b"\nendstream")
    )


def raw_pdf(
    *contents: bytes | None,
    resources: bytes = FONT,
    extra: Sequence[bytes] = (),
    boxes: bytes = b"/MediaBox [0 0 612 792]",
    catalog: bytes = b"",
) -> bytes:
    """One page per content stream (None: a page without `/Contents`), each with `resources` and
    `boxes`; `extra` objects follow the pages, numbered from `4 + 2 * pages` (`extra_number`);
    `catalog` is added to the catalog's dictionary."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R " + catalog + b">>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(b"%d 0 R" % (4 + 2 * i) for i in range(len(contents)))
        + b"] /Count %d >>" % len(contents),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for i, content in enumerate(contents):
        page_contents = b"" if content is None else b" /Contents %d 0 R" % (5 + 2 * i)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R "
            + boxes
            + b" /Resources << "
            + resources
            + b" >>"
            + page_contents
            + b" >>"
        )
        objects.append(stream(b"", content or b""))
    objects += extra
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def extra_number(index: int, pages: int = 1) -> int:
    return 4 + 2 * pages + index


def count(data: bytes, page: int = 0) -> int:
    reader = PdfReader(io.BytesIO(data))
    return hidden_runs(reader.pages[page], reader)


def shown(*operators: bytes) -> bytes:
    """`VISIBLE`, then one text object with `operators` before the run `(Hidden line) Tj`."""
    return VISIBLE + b"BT /F1 12 Tf 72 400 Td " + b" ".join(operators) + b" (Hidden line) Tj ET"


# ── Hidden ────────────────────────────────────────────────────────────────────────────────────


def test_ordinary_text_is_never_counted() -> None:
    assert count(raw_pdf(VISIBLE + b"BT /F1 12 Tf 14 TL 72 700 Td (a) Tj T* (b) Tj ET")) == 0


@pytest.mark.parametrize(
    "operators",
    [b"3 Tr", b"7 Tr", b"9 Tr", b"1.5 Tr"],
    ids=["invisible", "clip only", "out of range", "not an integer"],
)
def test_render_modes_that_paint_nothing_are_hidden(operators: bytes) -> None:
    assert count(raw_pdf(shown(operators))) == 1


@pytest.mark.parametrize(
    "operators",
    [
        b"1 g",
        b"0.97 g",
        b"1 1 1 rg",
        b"0 0 0 0 k",
        b"/DeviceRGB cs 1 1 1 sc",
        b"/DeviceGray cs 1 scn",
        b"/CS0 cs 1 1 1 scn",
        b"1 1 1 RG 1 Tr",
        b"1 g 1 G 2 Tr",
        b"1 g 4 Tr",
        b"1 G 5 Tr",
        b"1 sc",
        b"0.95 g",
    ],
    ids=[
        "gray",
        "near white",
        "rgb",
        "cmyk",
        "device rgb by cs",
        "device gray by cs",
        "icc based",
        "white stroke on stroked text",
        "white fill and stroke",
        "white fill, fill and clip",
        "white stroke, stroke and clip",
        "sc in the initial DeviceGray",
        "exactly NEAR_WHITE",
    ],
)
def test_white_text_is_hidden(operators: bytes) -> None:
    icc = stream(b"/N 3", b"")
    data = raw_pdf(
        shown(operators),
        resources=FONT + b" /ColorSpace << /CS0 [/ICCBased %d 0 R] >>" % extra_number(0),
        extra=[icc],
    )
    assert count(data) == 1


@pytest.mark.parametrize(
    "operators",
    [b"0.5 g", b"0 0 1 rg", b"/P0 scn", b"1 g 1 Tr", b"1 g 0 G 2 Tr", b"/Lab0 cs 100 0 0 sc"],
    ids=["grey", "blue", "pattern", "white fill, stroked", "black stroke", "unread space"],
)
def test_colours_that_are_seen_are_not_counted(operators: bytes) -> None:
    lab = b"/ColorSpace << /Lab0 [/Lab << /WhitePoint [0.95 1 1.09] >>] >>"
    assert count(raw_pdf(shown(operators), resources=FONT + b" " + lab)) == 0


SPACES: Final = (
    b"/ColorSpace << /Alias /DeviceRGB /Cal [/CalRGB << /WhitePoint [0.95 1 1.09] >>]"
    b" /CalG [/CalGray << /WhitePoint [0.95 1 1.09] >>] /Icc4 [/ICCBased %d 0 R]"
    b" /Odd 7 /Nested [[/ICCBased] 1] /Bad [/ICCBased %d 0 R] >>"
)
"""Named colour spaces: an alias of a device space, two calibrated ones, an ICC-based one with
four components, and three it cannot read."""


@pytest.mark.parametrize(
    ("operators", "hidden_runs"),
    [
        (b"/Alias cs 1 1 1 scn", 1),
        (b"/Cal cs 1 1 1 sc", 1),
        (b"/CalG cs 1 sc", 1),
        (b"/Icc4 cs", 1),  # its initial colour is all zeros: no ink (PDF 32000 §8.6.5.5)
        (b"/Icc4 cs 0 0 0 1 sc", 0),
        (b"/DeviceCMYK cs", 0),  # starts at black, 0 0 0 1
        (b"/Odd cs 1 1 1 sc", 0),
        (b"/Nested cs 1 1 1 sc", 0),
        (b"/Bad cs 1 1 1 sc", 0),
        (b"/Bad cs 1 1 sc", 0),
        (b"5 cs 1 1 1 sc", 0),
        (b"/Missing cs 1 sc", 0),
    ],
    ids=[
        "device alias",
        "calibrated",
        "calibrated gray",
        "icc n=4 starts white",
        "icc n=4 with black ink",
        "device cmyk starts black",
        "not an array",
        "unnamed family",
        "icc n=2",
        "icc n=2, two operands",
        "no name",
        "unknown",
    ],
)
def test_named_colour_spaces_are_read_by_their_components(
    operators: bytes, hidden_runs: int
) -> None:
    icc4, bad_icc = stream(b"/N 4", b""), stream(b"/N 2", b"")
    spaces = SPACES % (extra_number(0), extra_number(1))
    data = raw_pdf(shown(operators), resources=FONT + b" " + spaces, extra=[icc4, bad_icc])
    assert count(data) == hidden_runs


GSTATES: Final = (
    b"/ExtGState << /Clear << /ca 0 >> /Faint << /ca 0.05 >> /ClearStroke << /CA 0 >>"
    b" /Half << /ca 0.5 >> /Named << /ca /Zero >> /Odd 7 >>"
)
"""ExtGStates: a clear fill, a fill at exactly `NEAR_CLEAR`, a clear stroke, a half-transparent
fill, and two it cannot read."""


@pytest.mark.parametrize(
    ("operators", "hidden_runs"),
    [
        (b"/Clear gs", 1),
        (b"/Faint gs", 1),
        (b"/ClearStroke gs 1 Tr", 1),
        (b"/Clear gs /ClearStroke gs 2 Tr", 1),
        (b"/Half gs", 0),
        (b"/ClearStroke gs", 0),
        (b"/Clear gs 1 Tr", 0),
        (b"/Clear gs 2 Tr", 0),
        (b"/Named gs /Odd gs /Missing gs 5 gs gs", 0),
    ],
    ids=[
        "clear fill",
        "exactly NEAR_CLEAR",
        "clear stroke on stroked text",
        "clear fill and stroke",
        "half transparent",
        "clear stroke on filled text",
        "clear fill on stroked text",
        "clear fill, stroke seen",
        "malformed states skipped",
    ],
)
def test_text_drawn_with_a_clear_alpha_is_hidden(operators: bytes, hidden_runs: int) -> None:
    assert count(raw_pdf(shown(operators), resources=FONT + b" " + GSTATES)) == hidden_runs


def test_td_moves_by_its_own_leading_and_malformed_moves_are_skipped() -> None:
    # `TD` sets the leading to -ty: the next `'` goes 15 points further down, off the page.
    assert count(raw_pdf(b"BT /F1 12 Tf 72 20 Td 0 -15 TD (On) Tj (Below) ' ET")) == 1
    assert count(raw_pdf(b"BT /F1 12 Tf 72 400 Td 72 Td x TL /A Tr (Kept) Tj ET")) == 0


def test_a_page_without_contents_has_nothing_hidden() -> None:
    assert count(raw_pdf(None)) == 0


def test_a_restored_state_is_not_hidden() -> None:
    content = VISIBLE + b"q 1 g 3 Tr Q BT /F1 12 Tf 72 400 Td (Seen again) Tj ET"
    assert count(raw_pdf(content)) == 0
    content = VISIBLE + b"q /Clear gs Q BT /F1 12 Tf 72 400 Td (Seen again) Tj ET"
    assert count(raw_pdf(content, resources=FONT + b" " + GSTATES)) == 0


@pytest.mark.parametrize(
    "content",
    [
        b"BT /F1 12 Tf 72 -40 Td (Below) Tj ET",
        b"BT /F1 12 Tf 900 400 Td (Right) Tj ET",
        b"BT /F1 12 Tf 1 0 0 1 -500 400 Tm (Left) Tj ET",
        b"q 1 0 0 1 0 2000 cm BT /F1 12 Tf 72 400 Td (Above) Tj ET Q",
        b"BT /F1 12 Tf 14 TL 72 10 Td (Last row) Tj (Next row) ' ET",
        b'BT /F1 12 Tf 14 TL 72 10 Td (Last row) Tj 0 0 (Next row) " ET',
        b"BT /F1 12 Tf 800 TL 72 700 Td T* (Next line) Tj ET",
    ],
    ids=[
        "td below",
        "td right",
        "tm left",
        "cm above",
        "quote moves down",
        "double quote",
        "t star moves down",
    ],
)
def test_a_run_that_starts_off_the_page_is_hidden(content: bytes) -> None:
    assert count(raw_pdf(VISIBLE + content)) == 1


@pytest.mark.parametrize(
    "content",
    [
        # a quarter turn by `cm`: (72, 400) lands at (212, 72)
        b"q 0 1 -1 0 612 0 cm BT /F1 12 Tf 72 400 Td (Rotated) Tj ET Q",
        # a quarter turn by `Tm`, then `Td` in the turned frame: (300, 300)
        b"BT /F1 12 Tf 0 1 -1 0 100 300 Tm 0 -200 Td (Turned) Tj ET",
        # `Tm` sets the line matrix too: `Td` moves from (72, 400), to (72, 386)
        b"BT /F1 12 Tf 1 0 0 1 72 400 Tm 0 -14 Td (Next row) Tj ET",
        # two `cm`s compose in order, the later one first: (72, 200) lands at (144, 100)
        b"q 1 0 0 1 0 -300 cm 2 0 0 2 0 0 cm BT /F1 12 Tf 72 200 Td (Scaled) Tj ET Q",
        # a new colour space starts at its initial colour: the white before it no longer applies
        b"1 g /DeviceGray cs BT /F1 12 Tf 72 400 Td (Black again) Tj ET",
    ],
    ids=["cm rotation", "tm rotation then td", "tm sets the line matrix", "cm order", "cs resets"],
)
def test_transformed_text_that_lands_on_the_page_is_not_hidden(content: bytes) -> None:
    assert count(raw_pdf(VISIBLE + content)) == 0


def test_the_crop_box_decides_not_the_media_box() -> None:
    boxes = b"/MediaBox [0 0 612 792] /CropBox [0 300 612 792]"
    assert count(raw_pdf(shown(), boxes=boxes)) == 0  # y 400 is inside the crop box
    content = VISIBLE + b"BT /F1 12 Tf 72 200 Td (Cropped) Tj ET"  # y 200 is below it
    assert count(raw_pdf(content, boxes=boxes)) == 1
    assert count(raw_pdf(content)) == 0


def test_a_box_given_by_its_other_corners_is_the_same_box() -> None:
    assert count(raw_pdf(VISIBLE, boxes=b"/MediaBox [612 792 0 0]")) == 0


def form(content: bytes, *, matrix: bytes = b"", resources: bytes = FONT) -> bytes:
    return stream(
        b"/Type /XObject /Subtype /Form /BBox [0 0 612 792] "
        + matrix
        + b" /Resources << "
        + resources
        + b" >>",
        content,
    )


def with_forms(page: bytes, *forms: bytes) -> bytes:
    names = b" ".join(b"/Fm%d %d 0 R" % (i, extra_number(i)) for i in range(len(forms)))
    return raw_pdf(page, resources=FONT + b" /XObject << " + names + b" >>", extra=forms)


def test_hidden_text_inside_a_form_is_counted_with_the_state_it_inherits() -> None:
    inside = b"BT /F1 12 Tf 72 400 Td (Inside a form) Tj ET"
    assert count(with_forms(VISIBLE + b"/Fm0 Do", form(inside))) == 0
    assert count(with_forms(VISIBLE + b"q 1 g /Fm0 Do Q", form(inside))) == 1  # white inherited
    assert count(with_forms(VISIBLE + b"/Fm0 Do", form(b"3 Tr " + inside))) == 1
    moved = form(inside, matrix=b"/Matrix [1 0 0 1 0 -1000]")
    assert count(with_forms(VISIBLE + b"/Fm0 Do", moved)) == 1  # its matrix moves it off
    # `Do` restores the state: the form's `3 Tr` never hides the page's next line.
    after = b"/Fm0 Do BT /F1 12 Tf 72 300 Td (After the form) Tj ET"
    assert count(with_forms(VISIBLE + after, form(b"3 Tr " + inside))) == 1


def test_a_forms_matrix_applies_before_the_ctm() -> None:
    # (72, 200) -> the form's /Matrix -> (72, -100) -> the CTM, x2 -> (144, -200): off the page.
    # In the other order it would land at (144, 100), on it.
    inside = form(b"BT /F1 12 Tf 72 200 Td (In the form) Tj ET", matrix=b"/Matrix [1 0 0 1 0 -300]")
    assert count(with_forms(VISIBLE + b"q 2 0 0 2 0 0 cm /Fm0 Do Q", inside)) == 1


def test_an_image_xobject_or_a_missing_one_draws_no_text() -> None:
    image = stream(
        b"/Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray"
        b" /BitsPerComponent 8",
        b"\x80",
    )
    assert count(with_forms(VISIBLE + b"q 3 Tr /Fm0 Do /Gone Do Q", image)) == 0


def test_a_form_too_deep_to_read_counts_as_hidden_and_a_cycle_stops() -> None:
    # Fm0 draws Fm1 draws Fm2 ...: the last level past FORM_DEPTH_MAX is not read (in doubt).
    depth = FORM_DEPTH_MAX + 1
    xobjects = b" ".join(b"/Fm%d %d 0 R" % (i, extra_number(i)) for i in range(depth))
    resources = FONT + b" /XObject << " + xobjects + b" >>"
    chain = [form(b"/Fm%d Do" % (i + 1), resources=resources) for i in range(depth - 1)]
    chain.append(form(b"BT /F1 12 Tf 72 400 Td (Deep) Tj ET"))
    assert count(raw_pdf(VISIBLE + b"/Fm0 Do", resources=resources, extra=chain)) == 1
    cycle = raw_pdf(
        VISIBLE + b"/Fm0 Do",
        resources=FONT + b" /XObject << /Fm0 %d 0 R >>" % extra_number(0),
        extra=[form(b"/Fm0 Do", resources=FONT + b" /XObject << /Fm0 %d 0 R >>" % extra_number(0))],
    )
    assert count(cycle) == 1


@pytest.mark.parametrize(
    "content",
    [
        b"BT /F1 12 Tf 3 Tr 72 400 Td (   ) Tj () Tj [( ) -250] TJ ET",
        b"BT /F1 12 Tf 3 Tr 72 400 Td [-250 () 120] TJ ET",
        b"BT /F1 12 Tf 3 Tr 72 400 Td Tj ET",
        b"BT /F1 12 Tf 72 400 Td (Kept) Tj 1 0 0 Tm /Fm9 Do 5 Do ET",
    ],
    ids=["whitespace", "numbers only", "no operand", "malformed operators skipped"],
)
def test_nothing_to_show_or_malformed_operators_count_nothing(content: bytes) -> None:
    assert count(raw_pdf(VISIBLE + content)) == 0


def test_a_tj_array_with_text_counts_once() -> None:
    content = b"BT /F1 12 Tf 3 Tr 72 400 Td [(Hid) -20 (den)] TJ ET"
    assert count(raw_pdf(VISIBLE + content)) == 1


# ── Bounded (G3) ──────────────────────────────────────────────────────────────────────────────


def test_past_the_operation_cap_the_walk_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hidden, "OPS_MAX", 50)
    with pytest.raises(OverCap, match="operations"):
        count(raw_pdf(VISIBLE * 20))
    # A form drawn many times counts every operation it runs, so it cannot multiply the walk.
    inside = form(b"BT /F1 12 Tf 72 400 Td (x) Tj ET")
    with pytest.raises(OverCap):
        count(with_forms(VISIBLE + b"/Fm0 Do " * 12, inside))
    assert count(with_forms(VISIBLE + b"/Fm0 Do " * 3, inside)) == 0


def test_the_operation_cap_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hidden, "OPS_MAX", 5)  # VISIBLE is 5 operations
    assert count(raw_pdf(VISIBLE)) == 0
    monkeypatch.setattr(hidden, "OPS_MAX", 4)
    with pytest.raises(OverCap):
        count(raw_pdf(VISIBLE))


def test_a_form_drawn_many_times_is_parsed_once_per_page(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cap bounds the walk; parsing a form on every `Do` would not be bounded by it.
    parsed: list[object] = []
    real = ContentStream

    def counted(stream: object, pdf: object) -> object:
        parsed.append(stream)
        return real(stream, pdf)

    monkeypatch.setattr(hidden, "ContentStream", counted)
    inside = form(b"BT /F1 12 Tf 72 400 Td (x) Tj ET")
    data = with_forms(VISIBLE + b"/Fm0 Do " * 40, inside)
    assert count(data) == 0
    assert len(parsed) == 1
    two_pages = raw_pdf(
        VISIBLE + b"/Fm0 Do /Fm0 Do",
        VISIBLE + b"/Fm0 Do",
        resources=FONT + b" /XObject << /Fm0 %d 0 R >>" % extra_number(0, pages=2),
        extra=[inside],
    )
    assert (count(two_pages, 0), count(two_pages, 1)) == (0, 0)
    assert len(parsed) == 3  # once on each page's walk


# ── Known gaps (G7's docstring list): not detected, pinned so a change is seen ───────────────


@pytest.mark.parametrize(
    "content",
    [
        b"BT /F1 0.1 Tf 72 400 Td (Tiny font) Tj ET",
        b"BT /F1 12 Tf 0 Tz 72 400 Td (No width) Tj ET",
        b"q 0 0 1 1 re W n BT /F1 12 Tf 72 400 Td (Clipped away) Tj ET Q",
        b"BT /F1 12 Tf 72 400 Td (Covered) Tj ET 1 g 60 390 300 30 re f",
        b"BT /F1 12 Tf 72 400 Td [-100000 (Moved off)] TJ ET",  # drawn from x = 1272
    ],
    ids=[
        "tiny font",
        "zero horizontal scaling",
        "clipping path",
        "white overlay",
        "tj displacement",
    ],
)
def test_known_gaps_are_not_detected(content: bytes) -> None:
    assert count(raw_pdf(VISIBLE + content)) == 0


def test_optional_content_off_by_default_is_a_known_gap() -> None:
    group = extra_number(0)
    data = raw_pdf(
        VISIBLE + b"/OC /oc1 BDC BT /F1 12 Tf 72 400 Td (In a layer that is off) Tj ET EMC",
        resources=FONT + b" /Properties << /oc1 %d 0 R >>" % group,
        extra=[b"<< /Type /OCG /Name (Layer) >>"],
        catalog=b"/OCProperties << /OCGs [%d 0 R] /D << /OFF [%d 0 R] >> >> " % (group, group),
    )
    assert "In a layer that is off" in read_pages(data).pages[0].text  # the screens still read it
    assert count(data) == 0


# ── What the screen reads (in the reading thread) ────────────────────────────────────────────


def test_read_for_screen_refuses_a_document_whose_pages_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    two_pages = read_pages(pdf(["One"], ["Two"]))
    monkeypatch.setattr(document, "read_pages", lambda data: two_pages)
    with pytest.raises(ValueError, match="changed between two reads"):
        read_for_screen(pdf(["Only one page"]))


def test_read_for_screen_keeps_the_extractions_own_text_and_counts_per_page() -> None:
    data = pdf(
        ["Plain first page"],
        [Line("Second page"), Line("Render mode three", hidden=True)],
        Page([Line("Third page"), Line("White line", white=True), Line("Far right", x=900)]),
    )

    pages = read_for_screen(data)

    assert [page.text for page in pages] == [page.text for page in read_pages(data).pages]
    assert [(page.number, page.hidden) for page in pages] == [(1, 0), (2, 1), (3, 2)]
    assert "Render mode three" in pages[1].text  # hidden text is screened like the rest
    assert "Render mode three" not in repr(pages[1])  # and never printed by a repr

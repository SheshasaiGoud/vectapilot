"""Hidden text on a PDF page, read from pypdf's content-stream operators (M7 PR 7; G3, G7).

`hidden_runs(page, reader)` counts the text-showing operations (`Tj`, `TJ`, `'`, `"`) with
something to show that a reader of the page would not see:
- **render mode** `3 Tr` (neither filled nor stroked), and `7 Tr` (added to the clipping path
  only), which paint nothing either; a mode outside 0-7 counts as hidden (in doubt);
- **white**: text filled (modes 0, 4), stroked (1, 5) or both (2, 6) in white or near white
  (`NEAR_WHITE`), in DeviceGray, DeviceRGB or DeviceCMYK, or in an ICC-based or calibrated colour
  space read by its number of components (an ICC-based space with four starts white: its initial
  colour is no ink, PDF 32000 §8.6.5.5);
- **transparent**: the fill or stroke alpha an ExtGState sets with `gs` (`/ca`, `/CA`) at or below
  `NEAR_CLEAR`, for the modes that paint with it (as white);
- **off-page**: the run's start point, through the text matrix and the current transformation
  matrix, lies outside the page's crop box.

Form XObjects drawn with `Do` are walked with the graphics state they inherit, at most
`FORM_DEPTH_MAX` levels deep (as `extraction.text` inspects them for images); a deeper form counts
as one hidden run (in doubt, flag). Each form is parsed once per page however often it is drawn,
and every operation walked, in forms too, counts towards `OPS_MAX` per page; over it raises
`OverCap` (G3: over a cap fails closed). The cap bounds the walk; pypdf's own decode limits bound
the parse, which keeps the whole page's operations in memory as M6's own reads do.

**A signal, never a decision** (G7): every page's whole text goes through the red-flag and
injection screens, hidden or not (`guards.document`); this only adds `document:hidden_text`.

**Known gaps** (not detected): tiny fonts (`Tf` near 0), horizontal scaling `0 Tz`, text clipped
away by a clipping path (`W n`), text covered by an overlay (a white rectangle drawn over it), text
in a colour that matches a non-white background, the colour spaces not listed above (Pattern,
Indexed, Separation, DeviceN, Lab), a soft mask or a blend mode, text that starts on the page and
runs off its edge (glyph widths are not tracked, so only a run's start point is placed), a run
moved off the page within itself (a `TJ` displacement, or large `Tc`/`Tw` spacing), the text rise
(`Ts`), optional content that is off by default (`/OC ... BDC` with the group off in
`/OCProperties`), and an annotation's appearance stream (a page with one already counts as an
image page, E4).

**Known false positives** (flagged, though a reader sees the text): white text on a dark fill (a
letterhead's banner or a table header); a searchable scan's OCR layer, drawn in `3 Tr` over the
page image, which flags every such page; and mode 7 text used as the clip of an image or gradient.
To staff, `document:hidden_text` means "text a reader may not see", not "an attack".

Pure: no I/O, no logging; it reads hostile bytes, so the caller keeps only an exception's type.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from pypdf import PageObject, PdfReader
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DictionaryObject,
    IndirectObject,
    NameObject,
    PdfObject,
    StreamObject,
)

from vectapilot_api.extraction.text import FORM_DEPTH_MAX
from vectapilot_api.guards.caps import OverCap

OPS_MAX: Final = 200_000
"""Content-stream operations walked per page, forms included [proposal]: a bound, not a
measurement (a dense text page has a few thousand)."""
OPS_OVER_CAP: Final = f"a page's content is longer than its cap of {OPS_MAX} operations"
NEAR_WHITE: Final = 0.95
"""A colour component at least this light (or, in CMYK, an ink at most `1 - NEAR_WHITE`) counts
as white [proposal]: on a white page, 0.98 grey is as unreadable as 1.0."""
NEAR_CLEAR: Final = 0.05
"""An alpha at most this (`/ca`, `/CA`) counts as unseen [proposal], as near white does."""
INVISIBLE_MODES: Final = frozenset({3, 7})
FILL_MODES: Final = frozenset({0, 4})
STROKE_MODES: Final = frozenset({1, 5})
SHOWING: Final = frozenset({b"Tj", b"TJ", b"'", b'"'})
DEVICE_COMPONENTS: Final = {
    "/DeviceGray": 1,
    "/G": 1,
    "/DeviceRGB": 3,
    "/RGB": 3,
    "/DeviceCMYK": 4,
    "/CMYK": 4,
}
"""Device colour spaces by name (the short names are an inline image's, accepted by readers)."""
CALIBRATED: Final = {"/CalGray": 1, "/CalRGB": 3}

Operations = Sequence[tuple[Any, bytes]]
"""Parsed content-stream operations, as pypdf gives them: (operands, operator)."""
Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Final[Matrix] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
Box = tuple[float, float, float, float]
"""left, bottom, right, top."""


def _times(m: Matrix, n: Matrix) -> Matrix:
    """The product `m` times `n` in PDF's row-vector convention (`cm` makes the new CTM `m` times
    the CTM)."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a * a2 + b * c2,
        a * b2 + b * d2,
        c * a2 + d * c2,
        c * b2 + d * d2,
        e * a2 + f * c2 + e2,
        e * b2 + f * d2 + f2,
    )


def _resolve(item: PdfObject | None) -> PdfObject | None:
    return item.get_object() if isinstance(item, IndirectObject) else item


def _numbers(operands: Sequence[object], count: int | None = None) -> tuple[float, ...] | None:
    """The operands as floats, or None when one is not a number (or their count is not `count`):
    a malformed operator is skipped, as a reader would."""
    if count is not None and len(operands) != count:
        return None
    if not all(isinstance(x, int | float) and not isinstance(x, bool) for x in operands):
        return None
    return tuple(float(x) for x in operands if isinstance(x, int | float))


def _matrix(operands: Sequence[object]) -> Matrix | None:
    m = _numbers(operands, 6)
    return None if m is None else (m[0], m[1], m[2], m[3], m[4], m[5])


def _white(values: Sequence[float]) -> bool:
    """Gray or RGB components all near 1, or CMYK inks all near 0 (callers pass 1, 3 or 4)."""
    if len(values) == 4:
        return all(v <= 1 - NEAR_WHITE for v in values)
    return all(v >= NEAR_WHITE for v in values)


def _has_text(item: object) -> bool:
    if isinstance(item, str | bytes):
        return bool(item.strip())
    return False


def _shows(operator: bytes, operands: Sequence[object]) -> bool:
    """Whether a text-showing operation has something to show (whitespace alone is nothing)."""
    if operator == b"TJ":
        return (
            bool(operands)
            and isinstance(operands[0], ArrayObject)
            and any(_has_text(item) for item in operands[0])
        )
    return bool(operands) and _has_text(operands[-1])


@dataclass
class _Graphics:
    """The parts of the graphics state that decide whether text is seen (saved by `q`)."""

    ctm: Matrix = IDENTITY
    mode: int = 0
    leading: float = 0.0
    fill_components: int | None = 1
    stroke_components: int | None = 1
    fill_white: bool = False
    stroke_white: bool = False
    """The initial colour is black (DeviceGray); a `cs` sets its space's initial colour (`_space`:
    black, but for an ICC-based space with four components, which starts white)."""
    fill_alpha: float = 1.0
    stroke_alpha: float = 1.0

    def unseen(self) -> bool:
        if self.mode not in range(8) or self.mode in INVISIBLE_MODES:
            return True
        fill = self.fill_white or self.fill_alpha <= NEAR_CLEAR
        stroke = self.stroke_white or self.stroke_alpha <= NEAR_CLEAR
        if self.mode in FILL_MODES:
            return fill
        if self.mode in STROKE_MODES:
            return stroke
        return fill and stroke


UNREAD: Final = (None, False)


def _space(space: object, resources: DictionaryObject | None) -> tuple[int | None, bool]:
    """How many components a colour space named `space` has, if it is one this module reads
    (else None), and whether its initial colour is white: only an ICC-based space with four
    components, whose initial colour is all zeros, no ink (PDF 32000 §8.6.5.5; DeviceCMYK starts
    at black)."""
    if not isinstance(space, NameObject):
        return UNREAD
    if space in DEVICE_COMPONENTS:
        return DEVICE_COMPONENTS[space], False
    spaces = _resolve(resources.get("/ColorSpace")) if resources is not None else None
    entry = _resolve(spaces.get(space)) if isinstance(spaces, DictionaryObject) else None
    if isinstance(entry, NameObject):
        return DEVICE_COMPONENTS.get(entry), False
    if not isinstance(entry, ArrayObject) or not entry:
        return UNREAD
    family = _resolve(entry[0])
    if not isinstance(family, NameObject):
        return UNREAD
    if family in CALIBRATED:
        return CALIBRATED[family], False
    if family == "/ICCBased" and len(entry) > 1:
        stream = _resolve(entry[1])
        n = stream.get("/N") if isinstance(stream, StreamObject) else None
        return (int(n), n == 4) if isinstance(n, int) and n in (1, 3, 4) else UNREAD
    return UNREAD


def _alpha(item: PdfObject | None) -> float | None:
    value = _numbers((_resolve(item),), 1)
    return None if value is None else value[0]


class _Walk:
    """One page's walk: the hidden runs found and the operations counted so far."""

    def __init__(self, reader: PdfReader, box: Box) -> None:
        self._reader = reader
        self._box = box
        self._forms: dict[int, tuple[StreamObject, Operations]] = {}
        """Each form's operations by object, parsed once per page (the object is kept so its id
        is never reused)."""
        self.operations = 0
        self.hidden = 0

    def run(
        self,
        operations: Operations,
        resources: DictionaryObject | None,
        state: _Graphics,
        depth: int,
    ) -> None:
        saved: list[_Graphics] = []
        tm = tlm = IDENTITY
        for operands, operator in operations:
            self.operations += 1
            if self.operations > OPS_MAX:
                raise OverCap(OPS_OVER_CAP)
            if operator == b"q":
                saved.append(replace(state))
            elif operator == b"Q":
                state = saved.pop() if saved else state
            elif operator == b"cm":
                m = _matrix(operands)
                state.ctm = state.ctm if m is None else _times(m, state.ctm)
            elif operator == b"BT":
                tm = tlm = IDENTITY
            elif operator == b"Tm":
                m = _matrix(operands)
                tm = tlm = tm if m is None else m
            elif operator in (b"Td", b"TD"):
                t = _numbers(operands, 2)
                if t is not None:
                    if operator == b"TD":
                        state.leading = -t[1]
                    tm = tlm = _times((1.0, 0.0, 0.0, 1.0, t[0], t[1]), tlm)
            elif operator == b"T*":
                tm = tlm = _times((1.0, 0.0, 0.0, 1.0, 0.0, -state.leading), tlm)
            elif operator == b"TL":
                leading = _numbers(operands, 1)
                state.leading = state.leading if leading is None else leading[0]
            elif operator == b"Tr":
                mode = _numbers(operands, 1)
                state.mode = state.mode if mode is None else self._mode(mode[0])
            elif operator in SHOWING:
                if operator in (b"'", b'"'):  # T* first
                    tm = tlm = _times((1.0, 0.0, 0.0, 1.0, 0.0, -state.leading), tlm)
                if _shows(operator, operands) and (state.unseen() or self._off_page(tm, state)):
                    self.hidden += 1
            elif operator == b"Do":
                self._form(operands, resources, state, depth)
            elif operator == b"gs":
                self._transparency(operands, resources, state)
            else:
                self._colour(operator, operands, resources, state)

    @staticmethod
    def _mode(value: float) -> int:
        """A render mode; anything but an integer 0-7 is 3 (in doubt, hidden)."""
        return int(value) if value.is_integer() and 0 <= value <= 7 else 3

    def _off_page(self, tm: Matrix, state: _Graphics) -> bool:
        a, b, c, d, e, f = state.ctm
        x = tm[4] * a + tm[5] * c + e
        y = tm[4] * b + tm[5] * d + f
        left, bottom, right, top = self._box
        return not (left <= x <= right and bottom <= y <= top)  # NaN and inf are off the page

    @staticmethod
    def _colour(
        operator: bytes,
        operands: Sequence[object],
        resources: DictionaryObject | None,
        state: _Graphics,
    ) -> None:
        stroke = operator.isupper()
        components: int | None
        if operator in (b"g", b"G", b"rg", b"RG", b"k", b"K"):
            values = _numbers(operands)
            components = {b"g": 1, b"rg": 3, b"k": 4}[operator.lower()]
            white = values is not None and len(values) == components and _white(values)
        elif operator in (b"cs", b"CS"):  # a new colour space starts at its initial colour
            components, white = _space(operands[0] if operands else None, resources)
        elif operator in (b"sc", b"scn", b"SC", b"SCN"):
            components = state.stroke_components if stroke else state.fill_components
            values = _numbers(operands)  # a pattern's name makes this None: not white
            white = values is not None and len(values) == components and _white(values)
        else:
            return
        if stroke:
            state.stroke_components, state.stroke_white = components, white
        else:
            state.fill_components, state.fill_white = components, white

    @staticmethod
    def _transparency(
        operands: Sequence[object], resources: DictionaryObject | None, state: _Graphics
    ) -> None:
        """`gs`: the fill (`/ca`) and stroke (`/CA`) alpha of the named ExtGState, where set."""
        states = _resolve(resources.get("/ExtGState")) if resources is not None else None
        name = operands[0] if operands else None
        if not isinstance(name, NameObject) or not isinstance(states, DictionaryObject):
            return  # a malformed `gs` changes nothing
        entry = _resolve(states.get(name))
        if not isinstance(entry, DictionaryObject):
            return
        fill, stroke = _alpha(entry.get("/ca")), _alpha(entry.get("/CA"))
        state.fill_alpha = state.fill_alpha if fill is None else fill
        state.stroke_alpha = state.stroke_alpha if stroke is None else stroke

    def _parsed(self, xobject: StreamObject) -> Operations:
        """A form's operations, parsed on its first `Do` on this page only."""
        cached = self._forms.get(id(xobject))
        if cached is None:
            cached = self._forms[id(xobject)] = (
                xobject,
                ContentStream(xobject, self._reader).operations,
            )
        return cached[1]

    def _form(
        self,
        operands: Sequence[object],
        resources: DictionaryObject | None,
        state: _Graphics,
        depth: int,
    ) -> None:
        xobjects = _resolve(resources.get("/XObject")) if resources is not None else None
        name = operands[0] if operands else None
        if not isinstance(name, NameObject) or not isinstance(xobjects, DictionaryObject):
            return  # a malformed `Do` draws nothing
        xobject = _resolve(xobjects.get(name))
        if not isinstance(xobject, StreamObject) or xobject.get("/Subtype") != "/Form":
            return
        if depth >= FORM_DEPTH_MAX:
            self.hidden += 1  # unread: in doubt, flag
            return
        inner = replace(state)  # `Do` saves and restores the state around the form
        raw = _resolve(xobject.get("/Matrix"))
        matrix = _matrix(raw) if isinstance(raw, ArrayObject) else None
        if matrix is not None:
            inner.ctm = _times(matrix, state.ctm)
        own = _resolve(xobject.get("/Resources"))
        form_resources = own if isinstance(own, DictionaryObject) else resources
        self.run(self._parsed(xobject), form_resources, inner, depth + 1)


def page_box(page: PageObject) -> Box:
    """The page's crop box (pypdf falls back to the media box), as left, bottom, right, top.
    Always finite: pypdf refuses a number too long to be one."""
    box = page.cropbox
    xs = sorted((float(box.left), float(box.right)))
    ys = sorted((float(box.bottom), float(box.top)))
    return xs[0], ys[0], xs[1], ys[1]


def hidden_runs(page: PageObject, reader: PdfReader) -> int:
    """How many text runs on `page` a reader would not see (module docstring). Raises `OverCap`
    past `OPS_MAX` operations, and whatever pypdf raises on hostile bytes."""
    walk = _Walk(reader, page_box(page))
    resources = _resolve(page.get("/Resources"))
    contents = page.get_contents()
    walk.run(
        () if contents is None else contents.operations,
        resources if isinstance(resources, DictionaryObject) else None,
        _Graphics(),
        0,
    )
    return walk.hidden

"""Page text for extraction, with an OCR seam (ADR 0017 decision 1, decided D1 (a); E4, E12;
M6 PR 2).

PDF only, page by page with pypdf, in a reader of its own: `ingestion/loaders.py` stays untouched
while M2/M3's live runs are pending (plan, "Out of scope").

**Limits refuse, never cut (E12).** At most `MAX_BYTES`, `MAX_PAGES` and `MAX_CHARACTERS` of page
text [proposal]. Over any limit, `read_pages` raises `DocumentRefused` before any model call: the
document is never shortened to fit, because a field on a cut page would look absent (E4).

**What a page records.** Its 1-based number, its text (pypdf's text layer, line ends normalised,
NFC), whether it has text and whether it has images. "Images" means anything that may show text
pypdf's text layer does not hold, found with pypdf alone: no image is decoded or rendered (no
imaging package, rule 11):
- an image XObject in the page's resources (also inside a Form XObject, to a bounded depth) or an
  inline image (`BI ... EI`) in a content stream;
- an annotation that may draw: anything but a `/Link` or `/Popup`, or any with an appearance
  stream (`/AP`), such as a stamp holding a scanned insurance card (pypdf never reads `/AP`);
- on a page without text, any painting operator (`DRAWING`): text outlined into vector paths, or
  glyphs pypdf cannot map, look like a blank page otherwise. A page with no content stays blank.
An image listed in the resources but never drawn still counts, and a structure too deep or too
wide to inspect counts as an image: unseen text is never "absent" (E4), so in doubt a page has an
image. Known gap (ADR 0017): part of a value outlined on a page that also has text.

pypdf also reads invisible, white and off-page text; a hidden copy that conflicts with a visible
one is PR 2's `conflicting_values`, a single hidden copy is ADR 0017's known gap.

**The OCR seam (D1 (a)).** `OcrEngine` reads the text of a page that has images and no text layer
(a scan). Production uses `NoOcr`, which never recognises anything, so such a page keeps no text
and its fields go to review (E4); tests use a fake. A page whose text came from an engine is
marked `ocr`, which caps its values at `medium`. A real engine is a new dependency (rule 11) and
waits for a real business that sends scans (ADR 0017, "Revisit when").

**Uploaded documents are untrusted data (rule 5), and nothing here logs.** A parser fed hostile
bytes can fail in many ways (pypdf's own errors, but also `KeyError`, `ValueError`,
`RecursionError`, ...), and its message may quote the file. So only the exception's type name is
kept, and `DocumentRefused` is raised outside every `except` block: nothing is chained, because
the JSON log formatter writes a chained exception's full text (`log.py`). `Exception`, never
`BaseException`: cancellation always propagates. An engine's own exception is not caught here; the
service turns it into an all-review extraction (E3).

**pypdf's own log lines are silenced (E9).** pypdf logs warnings that quote the file's bytes (a
duplicated dictionary key, an unknown encoding name: tested), so a hostile PDF could write into
our logs. `log.silence_untrusted_parsers` raises the `pypdf` logger's level above `CRITICAL`, so
every pypdf record is dropped before any handler. It runs at startup (`configure_logging`) for
ingestion and extraction alike, and again when this module is imported (decided 2026-11-13).
"""

import hashlib
import io
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol

from pypdf import PageObject, PdfReader
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DictionaryObject,
    IndirectObject,
    PdfObject,
    StreamObject,
)

from vectapilot_api.log import silence_untrusted_parsers

MAX_BYTES: Final = 5 * 1024 * 1024
"""E12 [proposal]: an upload larger than this is refused unread."""
MAX_PAGES: Final = 5
"""E12 [proposal]: a document with more pages is refused before any page is read."""
MAX_CHARACTERS: Final = 20_000
"""E12 [proposal]: the pages' text (after normalisation, OCR text included), summed."""
PDF_MAGIC: Final = b"%PDF-"
MAGIC_WINDOW: Final = 1024
"""PDF readers accept the header anywhere in the first 1024 bytes."""
FORM_DEPTH_MAX: Final = 4
"""Nested Form XObjects inspected for images; deeper counts as an image (fail closed)."""
XOBJECTS_MAX: Final = 64
"""XObjects inspected per page; more counts as an image (fail closed)."""

silence_untrusted_parsers()  # also at startup (log.configure_logging); idempotent

INLINE_IMAGE: Final = b"INLINE IMAGE"
"""pypdf's operator name for a `BI ... ID ... EI` inline image in a parsed content stream."""
DRAWING: Final = frozenset(
    {b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"S", b"s", b"sh", b"Do"}
    | {b"Tj", b"TJ", b"'", b'"', INLINE_IMAGE}
)
"""Operators that paint something: paths, shadings, XObjects, inline images and text. On a page
without a text layer, any of them may be text a human sees (outlined glyphs, a font pypdf cannot
map), so the page counts as an image (E4)."""
PLAIN_ANNOTATIONS: Final = frozenset({"/Link", "/Popup"})
"""Annotations that draw nothing of their own unless they carry an appearance (`/AP`)."""


class RefusalReason(StrEnum):
    """Why a document is refused before extraction. Each is a fixed code; none carries text."""

    NOT_PDF = "not_pdf"
    ENCRYPTED = "encrypted"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too_large"
    TOO_MANY_PAGES = "too_many_pages"
    TOO_MUCH_TEXT = "too_much_text"


OVER_LIMIT: Final = frozenset(
    {RefusalReason.TOO_LARGE, RefusalReason.TOO_MANY_PAGES, RefusalReason.TOO_MUCH_TEXT}
)
"""E12's refusals (PR 3's 413); the others are a document that cannot be read."""


class DocumentRefused(Exception):
    """The document is refused before any model call. The message is fixed text, numbers and at
    most an exception's type name: never document text."""

    def __init__(self, reason: RefusalReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason

    @property
    def over_limit(self) -> bool:
        return self.reason in OVER_LIMIT


@dataclass(frozen=True)
class TextLimits:
    """E12's limits; the defaults are the proposal, tests pass smaller ones."""

    max_bytes: int = MAX_BYTES
    max_pages: int = MAX_PAGES
    max_characters: int = MAX_CHARACTERS

    def __post_init__(self) -> None:
        if min(self.max_bytes, self.max_pages, self.max_characters) < 1:
            raise ValueError("every text limit is at least 1")


DEFAULT_LIMITS: Final = TextLimits()


@dataclass(frozen=True)
class PageText:
    """One page: its number (from 1), its text and what it holds."""

    number: int
    text: str = field(repr=False)
    has_images: bool
    ocr: bool = False
    """The text came from an `OcrEngine`, not from the text layer (caps values at `medium`)."""

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("page numbers start at 1")
        if self.ocr and not self.has_images:
            raise ValueError("only a page with images is read by an OCR engine")

    @property
    def has_text(self) -> bool:
        """Whether the page shows any text at all (whitespace is not text)."""
        return bool(self.text.strip())


@dataclass(frozen=True)
class DocumentText:
    """Every page of one document, in order, and the file's SHA-256 (the bytes are never kept)."""

    pages: tuple[PageText, ...]
    sha256: str

    def __post_init__(self) -> None:
        if not self.pages:
            raise ValueError("a document has at least one page")
        if [page.number for page in self.pages] != list(range(1, len(self.pages) + 1)):
            raise ValueError("pages are numbered 1, 2, ... in order")

    @property
    def characters(self) -> int:
        return sum(len(page.text) for page in self.pages)

    @property
    def image_pages(self) -> tuple[int, ...]:
        """The numbers of the pages with images (E4)."""
        return tuple(page.number for page in self.pages if page.has_images)


class OcrEngine(Protocol):
    """The OCR seam (D1 (a)): the text of one page of `document`, or None if the engine reads
    nothing. Called only for a page with images and no text layer. Never logs the text."""

    def recognize(self, document: bytes, page_number: int) -> str | None: ...


class NoOcr:
    """Production's engine in M6: recognises nothing, so a scanned page's fields go to review."""

    def recognize(self, document: bytes, page_number: int) -> str | None:
        return None


NO_OCR: Final = NoOcr()


def normalize(text: str) -> str:
    """Line ends as `\\n`, no NUL characters, NFC (as ingestion's loader; E2 folds further)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return unicodedata.normalize("NFC", text)


def _resolve(item: PdfObject | None) -> PdfObject | None:
    return item.get_object() if isinstance(item, IndirectObject) else item


def _xobjects(resources: PdfObject | None) -> DictionaryObject | None:
    resolved = _resolve(resources)
    if not isinstance(resolved, DictionaryObject):
        return None
    xobjects = _resolve(resolved.get("/XObject"))
    return xobjects if isinstance(xobjects, DictionaryObject) else None


def _has_inline_image(contents: ContentStream | None) -> bool:
    return contents is not None and any(
        operator == INLINE_IMAGE for _, operator in contents.operations
    )


def _paints(contents: ContentStream | None) -> bool:
    return contents is not None and any(operator in DRAWING for _, operator in contents.operations)


def _draws_annotation(page: PageObject) -> int | None:
    """How many annotations were inspected, or None if one may draw: anything but a `/Link` or
    `/Popup`, or any annotation with an appearance (`/AP`), which pypdf's text never reads (a
    stamp holding a scanned card). An unreadable entry counts as drawing (fail closed)."""
    annotations = _resolve(page.get("/Annots"))
    if annotations is None:
        return 0
    if not isinstance(annotations, ArrayObject):
        return None
    for count, item in enumerate(annotations, start=1):
        annotation = _resolve(item)
        if count > XOBJECTS_MAX or not isinstance(annotation, DictionaryObject):
            return None
        if "/AP" in annotation or annotation.get("/Subtype") not in PLAIN_ANNOTATIONS:
            return None
    return len(annotations)


def page_has_images(page: PageObject, reader: PdfReader, *, has_text: bool = True) -> bool:
    """Whether the page may show something pypdf's text does not hold (module docstring): an
    image XObject or an inline image, an annotation that draws (`_draws_annotation`), or, on a
    page without text (`has_text` False), any painting operator (`DRAWING`). Bounded: more than
    `XOBJECTS_MAX` XObjects and annotations together, or Form XObjects nested deeper than
    `FORM_DEPTH_MAX` (a cycle included), count as an image."""
    contents = page.get_contents()
    if _has_inline_image(contents) or (not has_text and _paints(contents)):
        return True
    annotations = _draws_annotation(page)
    if annotations is None:
        return True
    stack: list[tuple[PdfObject | None, int]] = [(page.get("/Resources"), 1)]
    inspected = annotations
    while stack:
        resources, depth = stack.pop()
        xobjects = _xobjects(resources)
        if xobjects is None:
            continue
        for reference in xobjects.values():
            inspected += 1
            if inspected > XOBJECTS_MAX or depth > FORM_DEPTH_MAX:
                return True
            xobject = _resolve(reference)
            if not isinstance(xobject, StreamObject):
                continue
            subtype = xobject.get("/Subtype")
            if subtype == "/Image":
                return True
            if subtype == "/Form":
                if _has_inline_image(ContentStream(xobject, reader)):
                    return True
                stack.append((xobject.get("/Resources"), depth + 1))
    return False


def _parse(data: bytes, limits: TextLimits) -> tuple[tuple[str, bool], ...] | DocumentRefused:
    """Each page's text and whether it has images, or the refusal (returned, not raised, so the
    caller raises it outside its `except`). pypdf may raise anything here (hostile bytes)."""
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        return DocumentRefused(RefusalReason.ENCRYPTED, "encrypted PDFs are not supported")
    count = len(reader.pages)
    if count > limits.max_pages:
        return DocumentRefused(
            RefusalReason.TOO_MANY_PAGES, f"{count} pages; the limit is {limits.max_pages}"
        )
    if count == 0:
        return DocumentRefused(RefusalReason.UNREADABLE, "the PDF has no pages")
    pages: list[tuple[str, bool]] = []
    for page in reader.pages:
        text = normalize(page.extract_text())
        pages.append((text, page_has_images(page, reader, has_text=bool(text.strip()))))
    return tuple(pages)


def read_pages(
    data: bytes, *, limits: TextLimits = DEFAULT_LIMITS, ocr: OcrEngine = NO_OCR
) -> DocumentText:
    """Every page's text and images, or `DocumentRefused` (module docstring). E12's checks in
    order: size before parsing, page count before reading a page, characters after reading."""
    if len(data) > limits.max_bytes:
        raise DocumentRefused(
            RefusalReason.TOO_LARGE, f"{len(data)} bytes; the limit is {limits.max_bytes}"
        )
    if PDF_MAGIC not in data[:MAGIC_WINDOW]:
        raise DocumentRefused(RefusalReason.NOT_PDF, "no PDF header")
    parsed: tuple[tuple[str, bool], ...] | DocumentRefused | None = None
    failure = ""
    try:
        parsed = _parse(data, limits)
    except Exception as error:  # hostile bytes: only the type is kept (module docstring)
        failure = type(error).__name__
    if parsed is None:
        raise DocumentRefused(RefusalReason.UNREADABLE, f"unreadable PDF ({failure})")
    if isinstance(parsed, DocumentRefused):
        raise parsed
    pages = tuple(
        _page(data, number, text, images, ocr)
        for number, (text, images) in enumerate(parsed, start=1)
    )
    document = DocumentText(pages, hashlib.sha256(data).hexdigest())
    if document.characters > limits.max_characters:
        raise DocumentRefused(
            RefusalReason.TOO_MUCH_TEXT,
            f"{document.characters} characters; the limit is {limits.max_characters}",
        )
    return document


def _page(data: bytes, number: int, text: str, images: bool, ocr: OcrEngine) -> PageText:
    """The page, read by `ocr` if it has images and no text layer (a scan)."""
    if images and not text.strip():
        recognized = ocr.recognize(data, number)
        if recognized is not None and recognized.strip():
            return PageText(number, normalize(recognized), has_images=True, ocr=True)
    return PageText(number, text, has_images=images)

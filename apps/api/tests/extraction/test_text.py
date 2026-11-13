"""M6 PR 2: page text, images and the OCR seam (`extraction/text.py`; D1 (a), E4, E9, E12).

Every document is made up. PDFs come from `pdf_builder` (the fixture writer) or, for structures
it does not write (an inline image, a Form XObject), from pypdf's own writer.
"""

import io
import logging
from collections.abc import Mapping
from typing import Final

import pytest
from pypdf import PageObject, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NumberObject,
    PdfObject,
)

from tests.extraction.fakes import CANARY, OUTLINED, ScriptedOcr, stamp_with_card
from tests.ingestion.pdf_builder import Line, Page, build_pdf
from vectapilot_api.extraction import text as text_module
from vectapilot_api.extraction.text import (
    DEFAULT_LIMITS,
    FORM_DEPTH_MAX,
    MAGIC_WINDOW,
    MAX_BYTES,
    MAX_CHARACTERS,
    MAX_PAGES,
    NO_OCR,
    OVER_LIMIT,
    XOBJECTS_MAX,
    DocumentRefused,
    DocumentText,
    NoOcr,
    OcrEngine,
    PageText,
    RefusalReason,
    TextLimits,
    read_pages,
)

LETTER: Final = ("Patient name: Asha Rao", "Date of birth: 14/03/1990")


def refusal(data: bytes, **limits: int) -> DocumentRefused:
    with pytest.raises(DocumentRefused) as caught:
        read_pages(data, limits=TextLimits(**limits))
    return caught.value


# ── pypdf-written structures the fixture writer does not make ──────────────────────────────────


def _image(writer: PdfWriter) -> IndirectObject:
    image = DecodedStreamObject()
    image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(1),
            NameObject("/Height"): NumberObject(1),
            NameObject("/ColorSpace"): NameObject("/DeviceGray"),
            NameObject("/BitsPerComponent"): NumberObject(8),
        }
    )
    image.set_data(b"\x80")
    return writer._add_object(image)


def _form(
    writer: PdfWriter, content: bytes, xobjects: Mapping[str, IndirectObject]
) -> DecodedStreamObject:
    form = DecodedStreamObject()
    form.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([NumberObject(0)] * 2 + [NumberObject(1)] * 2),
            NameObject("/Resources"): DictionaryObject(
                {
                    NameObject("/XObject"): DictionaryObject(
                        {NameObject(f"/{k}"): v for k, v in xobjects.items()}
                    )
                }
            ),
        }
    )
    form.set_data(content)
    return form


def written(
    content: bytes,
    xobjects: Mapping[str, IndirectObject] | None = None,
    writer: PdfWriter | None = None,
) -> bytes:
    """A one-page PDF from pypdf's `writer` (the one `xobjects` were added to) with `content`."""
    writer = PdfWriter() if writer is None else writer
    page = writer.add_blank_page(612, 792)
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    if xobjects:
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/XObject"): DictionaryObject(
                    {NameObject(f"/{k}"): v for k, v in xobjects.items()}
                )
            }
        )
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


SHOWS_TEXT: Final = b"BT /F1 12 Tf 72 720 Td (Insurer: Lotus) Tj ET"
"""Content that shows text pypdf reads, in `text_pdf`'s Helvetica `/F1`."""


def text_pdf(
    content: bytes,
    *,
    xobjects: Mapping[str, IndirectObject] | None = None,
    annots: PdfObject | None = None,
    writer: PdfWriter | None = None,
) -> bytes:
    """`written` with a Helvetica `/F1` (so `SHOWS_TEXT` reads as text), and the page's
    `/Annots` set to `annots` exactly as given (an array or not)."""
    writer = PdfWriter() if writer is None else writer
    page = writer.add_blank_page(612, 792)
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    if xobjects:
        resources[NameObject("/XObject")] = DictionaryObject(
            {NameObject(f"/{k}"): v for k, v in xobjects.items()}
        )
    page[NameObject("/Resources")] = resources
    if annots is not None:
        page[NameObject("/Annots")] = annots
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def form_pdf(depth: int, *, image_inside: bool = True, cycle: bool = False) -> bytes:
    """Forms nested `depth` deep, the innermost holding an image (or, with `cycle`, itself)."""
    writer = PdfWriter()
    inner: dict[str, IndirectObject] = {"Im1": _image(writer)} if image_inside else {}
    form = _form(writer, b"/Im1 Do" if image_inside else b"", inner)
    reference = writer._add_object(form)
    if cycle:
        form[NameObject("/Resources")][NameObject("/XObject")][NameObject("/Me")] = reference  # type: ignore[index]
    for _ in range(depth - 1):
        reference = writer._add_object(_form(writer, b"/Fm Do", {"Fm": reference}))
    page = writer.add_blank_page(612, 792)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Fm"): reference})}
    )
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ── Pages: text, has_text, has_images ──────────────────────────────────────────────────────────


def test_each_page_has_its_text_number_and_what_it_holds() -> None:
    data = build_pdf([LETTER, Page(["Logo page"], image=True), Page([], image=True), []])

    document = read_pages(data)

    assert [(p.number, p.text, p.has_text, p.has_images, p.ocr) for p in document.pages] == [
        (1, "\n".join(LETTER), True, False, False),
        (2, "Logo page", True, True, False),
        (3, "", False, True, False),
        (4, "", False, False, False),
    ]
    assert document.image_pages == (2, 3)
    assert document.characters == len("\n".join(LETTER)) + len("Logo page")


def test_the_hash_is_the_files_and_the_bytes_are_not_kept() -> None:
    import hashlib

    data = build_pdf([LETTER])
    document = read_pages(data)

    assert document.sha256 == hashlib.sha256(data).hexdigest()
    assert not any(isinstance(value, bytes) for value in vars(document).values())


def test_hidden_text_is_read_as_text() -> None:
    # ADR 0017's known gap: pypdf reads `3 Tr` text; PR 2's conflicting_values catches a conflict.
    data = build_pdf([[Line("Insurer: Shown"), Line("Insurer: Hidden", hidden=True)]])
    assert read_pages(data).pages[0].text == "Insurer: Shown\nInsurer: Hidden"


def test_line_ends_and_nul_characters_are_normalised() -> None:
    assert text_module.normalize("a\r\nb\rc\x00d") == "a\nb\ncd"
    assert text_module.normalize("é") == "é"  # NFC


def test_an_inline_image_counts() -> None:
    data = written(b"q 10 0 0 10 72 72 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x80 EI Q")
    assert read_pages(data).pages[0].has_images


def test_an_image_inside_a_form_counts() -> None:
    assert read_pages(form_pdf(2)).pages[0].has_images


def test_an_inline_image_inside_a_form_counts() -> None:
    writer = PdfWriter()
    form = writer._add_object(_form(writer, b"BI /W 1 /H 1 /CS /G /BPC 8 ID \x80 EI", {}))
    data = written(b"/Fm Do", {"Fm": form}, writer)
    assert read_pages(data).pages[0].has_images


def test_an_inline_image_inside_a_form_on_a_page_with_text_counts() -> None:
    # On a page with text, `Do` alone is no image (DRAWING applies only without text): only the
    # form's own inline image can make it count.
    writer = PdfWriter()
    form = writer._add_object(_form(writer, b"BI /W 1 /H 1 /CS /G /BPC 8 ID \x80 EI", {}))
    data = text_pdf(SHOWS_TEXT + b" /Fm Do", xobjects={"Fm": form}, writer=writer)
    page = read_pages(data).pages[0]
    assert (page.text.strip(), page.has_images) == ("Insurer: Lotus", True)


def test_a_form_without_an_image_on_a_page_with_text_does_not_count() -> None:
    writer = PdfWriter()
    form = writer._add_object(_form(writer, b"", {}))
    data = text_pdf(SHOWS_TEXT + b" /Fm Do", xobjects={"Fm": form}, writer=writer)
    page = read_pages(data).pages[0]
    assert (page.text.strip(), page.has_images) == ("Insurer: Lotus", False)


def test_a_form_without_an_image_does_not_count() -> None:
    assert not read_pages(form_pdf(2, image_inside=False)).pages[0].has_images


def test_forms_nested_too_deep_count_as_an_image() -> None:
    # Fail closed (E4): what cannot be inspected may hold unseen text.
    assert not read_pages(form_pdf(FORM_DEPTH_MAX, image_inside=False)).pages[0].has_images
    assert read_pages(form_pdf(FORM_DEPTH_MAX + 1, image_inside=False)).pages[0].has_images


def test_a_form_that_contains_itself_ends_and_counts_as_an_image() -> None:
    assert read_pages(form_pdf(1, image_inside=False, cycle=True)).pages[0].has_images


def test_too_many_xobjects_count_as_an_image() -> None:
    def with_forms(count: int) -> bytes:
        writer = PdfWriter()
        forms = {f"F{i}": writer._add_object(_form(writer, b"", {})) for i in range(count)}
        return written(b"", forms, writer)

    assert not read_pages(with_forms(XOBJECTS_MAX)).pages[0].has_images
    assert read_pages(with_forms(XOBJECTS_MAX + 1)).pages[0].has_images


# ── What pypdf's text never reads: annotations and outlined text (E4) ───────────────────────


def annotated(
    annotations: list[DictionaryObject], content: bytes = b"", writer: PdfWriter | None = None
) -> bytes:
    """`written`, with `annotations` on the page (each added as an indirect object)."""
    writer = PdfWriter() if writer is None else writer
    page = writer.add_blank_page(612, 792)
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/Annots")] = ArrayObject(writer._add_object(a) for a in annotations)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def link() -> DictionaryObject:
    return DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Link"),
            NameObject("/Rect"): ArrayObject([NumberObject(n) for n in (0, 0, 10, 10)]),
        }
    )


def test_a_stamp_drawing_a_scanned_card_on_a_text_page_counts() -> None:
    content = b"BT /F1 12 Tf 72 720 Td (Insurer: see card) Tj ET"
    writer = PdfWriter()
    page = read_pages(annotated([stamp_with_card(writer)], content, writer)).pages[0]
    assert page.has_images


@pytest.mark.parametrize("subtype", ["/FreeText", "/Widget", "/Stamp"])
def test_any_annotation_but_a_link_or_popup_counts_even_without_an_appearance(
    subtype: str,
) -> None:
    annotation = link()
    annotation[NameObject("/Subtype")] = NameObject(subtype)
    assert read_pages(annotated([annotation])).pages[0].has_images


def test_a_link_or_popup_without_an_appearance_does_not_count() -> None:
    popup = link()
    popup[NameObject("/Subtype")] = NameObject("/Popup")
    assert not read_pages(annotated([link(), popup])).pages[0].has_images


def test_a_link_with_an_appearance_counts() -> None:
    drawn = link()
    drawn[NameObject("/AP")] = DictionaryObject()
    assert read_pages(annotated([drawn])).pages[0].has_images


def test_too_many_annotations_count_as_an_image() -> None:
    assert not read_pages(annotated([link()] * XOBJECTS_MAX)).pages[0].has_images
    assert read_pages(annotated([link()] * (XOBJECTS_MAX + 1))).pages[0].has_images


def test_annotations_and_xobjects_share_one_bound() -> None:
    # XOBJECTS_MAX bounds annotations and XObjects together, not each on its own.
    def page(links: int, forms: int) -> bytes:
        writer = PdfWriter()
        xobjects = {f"F{i}": writer._add_object(_form(writer, b"", {})) for i in range(forms)}
        annots = ArrayObject(writer._add_object(link()) for _ in range(links))
        return text_pdf(b"", xobjects=xobjects, annots=annots, writer=writer)

    assert not read_pages(page(10, XOBJECTS_MAX - 10)).pages[0].has_images
    assert read_pages(page(10, XOBJECTS_MAX - 10 + 1)).pages[0].has_images


@pytest.mark.parametrize("annots", [link(), NumberObject(7)], ids=["dictionary", "number"])
def test_annotations_that_are_not_an_array_count_as_an_image(annots: PdfObject) -> None:
    # A malformed `/Annots` cannot be inspected, so it fails closed, even on a page with text.
    page = read_pages(text_pdf(SHOWS_TEXT, annots=annots)).pages[0]
    assert (page.text, page.has_images) == ("Insurer: Lotus", True)


def test_a_page_with_text_and_no_annotations_or_images_does_not_count() -> None:
    page = read_pages(text_pdf(SHOWS_TEXT, annots=ArrayObject())).pages[0]
    assert (page.text, page.has_images) == ("Insurer: Lotus", False)


def test_a_page_of_outlined_text_counts_and_a_truly_empty_page_does_not() -> None:
    outlined = read_pages(written(OUTLINED)).pages[0]
    assert (outlined.has_text, outlined.has_images) == (False, True)
    assert not read_pages(written(b"")).pages[0].has_images
    assert not read_pages(build_pdf([[]])).pages[0].has_images


@pytest.mark.parametrize(
    "content",
    [b"0 0 m 100 100 l S", b"BT /F1 12 Tf 72 720 Td ( ) Tj ET", b"/Fm Do"],
    ids=["stroke", "text_showing_no_text", "xobject"],
)
def test_a_page_without_text_that_paints_anything_counts(content: bytes) -> None:
    assert read_pages(written(content)).pages[0].has_images


def test_a_text_layer_of_only_whitespace_is_no_text_for_image_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pypdf gives '' for whitespace-only text today; a text layer of spaces and line breaks (as
    # some scanners write) must still let the page's painting count.
    monkeypatch.setattr(PageObject, "extract_text", lambda self, *args, **kwargs: " \n ")
    page = read_pages(written(b"0 0 m 100 100 l S")).pages[0]
    assert (page.has_text, page.has_images) == (False, True)


def test_rules_and_boxes_on_a_page_with_text_do_not_count() -> None:
    # A table's lines next to readable text: the text is there to check (known gap: a value
    # partly outlined on a page with text, ADR 0017).
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    stream = DecodedStreamObject()
    stream.set_data(b"0 0 m 100 100 l S BT /F1 12 Tf 72 720 Td (Insurer: Lotus) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    out = io.BytesIO()
    writer.write(out)
    read = read_pages(out.getvalue()).pages[0]
    assert (read.text, read.has_images) == ("Insurer: Lotus", False)


# ── E12: limits refuse, never cut ──────────────────────────────────────────────────────────────


def test_the_limits_are_the_plans_proposal() -> None:
    assert (MAX_BYTES, MAX_PAGES, MAX_CHARACTERS) == (5 * 1024 * 1024, 5, 20_000)
    assert TextLimits() == DEFAULT_LIMITS
    with pytest.raises(ValueError, match="at least 1"):
        TextLimits(max_pages=0)


def test_a_file_over_the_byte_limit_is_refused_unread() -> None:
    data = build_pdf([LETTER])

    assert read_pages(data, limits=TextLimits(max_bytes=len(data))).pages
    refused = refusal(data, max_bytes=len(data) - 1)
    assert (refused.reason, refused.over_limit) == (RefusalReason.TOO_LARGE, True)
    assert refusal(b"x" * (MAX_BYTES + 1)).reason is RefusalReason.TOO_LARGE  # never parsed


def test_six_pages_are_refused_five_are_read() -> None:
    assert len(read_pages(build_pdf([LETTER] * 5)).pages) == 5
    refused = refusal(build_pdf([LETTER] * 6))
    assert (refused.reason, str(refused)) == (
        RefusalReason.TOO_MANY_PAGES,
        "too_many_pages: 6 pages; the limit is 5",
    )


def test_21000_characters_are_refused_never_cut() -> None:
    line = "Filler " * 14  # 98 characters per line
    page = [line] * 43  # about 4 300 characters per page
    data = build_pdf([page] * 5)
    total = read_pages(data, limits=TextLimits(max_characters=10**6)).characters
    assert total > 21_000

    refused = refusal(data)
    assert refused.reason is RefusalReason.TOO_MUCH_TEXT and refused.over_limit
    assert read_pages(data, limits=TextLimits(max_characters=total)).characters == total
    assert refusal(data, max_characters=total - 1).reason is RefusalReason.TOO_MUCH_TEXT


def test_ocr_text_counts_towards_the_character_limit() -> None:
    data = build_pdf([Page([], image=True)])
    ocr = ScriptedOcr({1: "x" * 50})

    with pytest.raises(DocumentRefused) as caught:
        read_pages(data, limits=TextLimits(max_characters=49), ocr=ocr)
    assert caught.value.reason is RefusalReason.TOO_MUCH_TEXT


def test_only_the_three_limits_are_over_limit() -> None:
    assert {
        RefusalReason.TOO_LARGE,
        RefusalReason.TOO_MANY_PAGES,
        RefusalReason.TOO_MUCH_TEXT,
    } == OVER_LIMIT


# ── Unreadable, encrypted, not a PDF: types only, nothing chained, nothing logged ──────────────


def encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.append(io.BytesIO(build_pdf([[f"Name: {CANARY}"]])))
    owner = "made-up-owner"  # a fixture, not a secret: the PDF is only opened by its user
    writer.encrypt(user_password="", owner_password=owner, algorithm="RC4-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


HOSTILE: Final = {
    "not a pdf": (f"{CANARY} plain text".encode(), RefusalReason.NOT_PDF),
    "garbage": (f"%PDF-1.4\n{CANARY} (({CANARY} obj <<".encode(), RefusalReason.UNREADABLE),
    "cut short": (build_pdf([[f"Name: {CANARY}"]])[:200], RefusalReason.UNREADABLE),
    "no pages": (build_pdf([]), RefusalReason.UNREADABLE),
    "encrypted": (encrypted_pdf(), RefusalReason.ENCRYPTED),
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_a_refusal_holds_a_code_and_types_only(name: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    data, reason = HOSTILE[name]

    refused = refusal(data)

    assert refused.reason is reason and not refused.over_limit
    assert CANARY not in str(refused)
    assert refused.__cause__ is None and refused.__context__ is None
    assert CANARY not in caplog.text


@pytest.mark.parametrize("offset", [0, 100, MAGIC_WINDOW - len(b"%PDF-")])
def test_the_pdf_header_may_start_anywhere_in_the_first_1024_bytes(offset: int) -> None:
    assert MAGIC_WINDOW == 1024
    data = b" " * offset + build_pdf([LETTER])
    assert read_pages(data).pages[0].text == "\n".join(LETTER)


def test_a_header_that_ends_past_the_first_1024_bytes_is_not_a_pdf() -> None:
    # The header at byte 1020 ends at byte 1025: outside the window, although pypdf could read it.
    refused = refusal(b" " * (MAGIC_WINDOW - len(b"%PDF-") + 1) + build_pdf([LETTER]))
    assert refused.reason is RefusalReason.NOT_PDF


def test_an_unreadable_pdf_names_the_parsers_exception_type_only() -> None:
    assert str(refusal(HOSTILE["garbage"][0])) == "unreadable: unreadable PDF (PdfStreamError)"


@pytest.mark.parametrize(
    ("find", "replace"),
    [
        (b"/Pages 2 0 R >>", b"/Pages 2 0 R /Zq6canary 1 /Zq6canary 2 >>"),
        (b"/Encoding /WinAnsiEncoding", b"/Encoding /Zq6canaryEncoding"),
    ],
)
def test_pypdfs_own_warnings_never_reach_a_log(
    find: bytes, replace: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    # pypdf quotes a duplicated key or an unknown encoding name in its warnings (module docstring).
    caplog.set_level(logging.DEBUG)
    data = build_pdf([["Name: A"]]).replace(find, replace)

    assert read_pages(data).pages[0].text == "Name: A"
    assert "zq6canary" not in caplog.text.casefold()
    assert logging.getLogger("pypdf._reader").getEffectiveLevel() > logging.CRITICAL


# ── The OCR seam (D1 (a)) ──────────────────────────────────────────────────────────────────────


def test_production_ocr_reads_nothing_so_a_scan_keeps_no_text() -> None:
    assert isinstance(NO_OCR, NoOcr)
    engine: OcrEngine = NO_OCR
    assert engine.recognize(b"%PDF-", 1) is None

    page = read_pages(build_pdf([Page([], image=True)])).pages[0]
    assert (page.text, page.has_text, page.has_images, page.ocr) == ("", False, True, False)


def test_an_engine_reads_only_scanned_pages_and_its_text_is_marked() -> None:
    data = build_pdf([Page([], image=True), Page(["Typed"], image=True), [], Page([], image=True)])
    ocr = ScriptedOcr({1: "Patient name: Asha\r\nRao", 4: "   "})

    document = read_pages(data, ocr=ocr)

    assert ocr.asked == [1, 4]  # never a page with a text layer, never a page without images
    assert [(p.text, p.ocr) for p in document.pages] == [
        ("Patient name: Asha\nRao", True),
        ("Typed", False),
        ("", False),
        ("", False),  # whitespace from an engine is no text
    ]


def test_a_scan_whose_text_layer_is_only_whitespace_is_read_by_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Some scanners write a text layer of spaces and line breaks: still a scan, still read.
    monkeypatch.setattr(text_module, "_parse", lambda data, limits: (("  \n ", True),))
    ocr = ScriptedOcr({1: "Patient name: Asha Rao"})

    document = read_pages(build_pdf([Page([], image=True)]), ocr=ocr)

    assert ocr.asked == [1]
    assert [(p.text, p.ocr) for p in document.pages] == [("Patient name: Asha Rao", True)]


def test_page_and_document_types_check_themselves() -> None:
    with pytest.raises(ValueError, match="start at 1"):
        PageText(0, "", has_images=False)
    with pytest.raises(ValueError, match="with images"):
        PageText(1, "x", has_images=False, ocr=True)
    with pytest.raises(ValueError, match="at least one page"):
        DocumentText((), "0" * 64)
    with pytest.raises(ValueError, match="in order"):
        DocumentText((PageText(2, "", has_images=False),), "0" * 64)
    assert "Asha" not in repr(PageText(1, "Asha", has_images=False))

"""Fakes and builders for M6 PR 2's tests: the committed configuration, pages, an OCR engine.

Every document text here is made up (synthetic, ASCII, as `pdf_builder` writes). The canary
`CANARY` is written into documents and must never reach a log line, an exception's text or an
`llm_call` record.
"""

import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import Final

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from tests.extraction.helpers import CORPUS_DIR, EXTRACTION_DIR
from vectapilot_api.extraction.config import KindConfig
from vectapilot_api.extraction.files import ExtractionConfig, load_extraction_dir
from vectapilot_api.extraction.text import PageText

SUNRISE_ID: Final = "sunrise-clinic"
PAWS_ID: Final = "paws-and-co"
REFERRAL_KIND: Final = "referral_letter"
VACCINATION_KIND: Final = "vaccination_record"
TRACE: Final = "4bf92f3577b34da6a3ce929d0e0e4736"
CANARY: Final = "zq6canary"
"""Made up: a marker in document text that no log line or error may contain."""

REFERRAL_PAGE: Final = (
    "Sunrise Clinic referral (synthetic)",
    f"Patient name: Asha {CANARY.capitalize()}",
    "Date of birth: 14/03/1990",
    "Referring doctor: Dr Meera Iyer",
    "Referral date: 2 November 2026",
    "Insurer: Lotus Health Mutual",
    "Policy number: LHM-204117",
)
"""A made-up referral letter, one field per line."""


@cache
def committed_config() -> ExtractionConfig:
    """`data/extraction`, loaded as the API loads it."""
    return load_extraction_dir(EXTRACTION_DIR, CORPUS_DIR)


def kind_of(business_id: str, kind: str) -> KindConfig:
    found = committed_config().kind_config(business_id, kind)
    assert found is not None, (business_id, kind)
    return found


def referral() -> KindConfig:
    return kind_of(SUNRISE_ID, REFERRAL_KIND)


def vaccination() -> KindConfig:
    return kind_of(PAWS_ID, VACCINATION_KIND)


def pages(*texts: str, images: Sequence[int] = ()) -> tuple[PageText, ...]:
    """Pages numbered from 1; `images` lists the page numbers that have images."""
    return tuple(
        PageText(number, text, has_images=number in images)
        for number, text in enumerate(texts, start=1)
    )


@dataclass
class ScriptedOcr:
    """An OCR engine that reads `texts[page_number]` and records every page it was asked for."""

    texts: Mapping[int, str | None] = field(default_factory=dict)
    asked: list[int] = field(default_factory=list)

    def recognize(self, document: bytes, page_number: int) -> str | None:
        self.asked.append(page_number)
        return self.texts.get(page_number)


# ── PDF structures the fixture writer does not make (pypdf's own writer) ───────────────────────

OUTLINED: Final = b"10 10 m 20 40 l 30 10 l f"
"""A filled path: one glyph outlined into vector paths by a design tool (no text layer)."""


def stamp_with_card(writer: PdfWriter) -> DictionaryObject:
    """A /Stamp annotation whose appearance (`/AP /N`) draws a 1x1 image XObject: a scanned
    insurance card pasted onto a letter. pypdf's text never reads it."""
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
    appearance = DecodedStreamObject()
    appearance.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([NumberObject(0)] * 2 + [NumberObject(1)] * 2),
            NameObject("/Resources"): DictionaryObject(
                {
                    NameObject("/XObject"): DictionaryObject(
                        {NameObject("/Im1"): writer._add_object(image)}
                    )
                }
            ),
        }
    )
    appearance.set_data(b"q /Im1 Do Q")
    return DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Stamp"),
            NameObject("/Rect"): ArrayObject([NumberObject(n) for n in (72, 72, 272, 172)]),
            NameObject("/AP"): DictionaryObject({NameObject("/N"): writer._add_object(appearance)}),
        }
    )


def edited(data: bytes, *, stamp_on: int | None = None, outline: int | None = None) -> bytes:
    """`data` (a PDF) with a card stamp on page `stamp_on`, or page `outline`'s content replaced
    by `OUTLINED` (pages from 1)."""
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    if stamp_on is not None:
        page = writer.pages[stamp_on - 1]
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(stamp_with_card(writer))])
    if outline is not None:
        stream = DecodedStreamObject()
        stream.set_data(OUTLINED)
        writer.pages[outline - 1][NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()

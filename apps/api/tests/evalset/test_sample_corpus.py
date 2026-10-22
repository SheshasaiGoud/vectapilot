"""M2 PR 7: the committed sample corpus in data/sample/ (synthetic, two fictional businesses).

These check the corpus's shape and its safety labelling, not its wording. Every document is
loaded and chunked with the real ingestion code, as `POST /v1/kb/documents` would.
"""

import os
import re
from collections import Counter
from typing import Final

import pytest

from tests.evalset.helpers import BUSINESSES, CORPUS_DIR, real_corpus
from tests.ingestion.pdf_builder import build_pdf
from vectapilot_api.evalset.corpus import PDF_TEXT_SUFFIX, CorpusDocument
from vectapilot_api.evalset.files import read_business, unlisted_files
from vectapilot_api.evalset.personal_data import (
    RESERVED_DOMAIN,
    URL_HOST,
    personal_data_findings,
)
from vectapilot_api.ingestion.chunking import chunk_document
from vectapilot_api.ingestion.types import ChunkingConfig, SourceType
from vectapilot_api.kb.types import ContentKind

UPDATE: Final = os.environ.get("UPDATE_SAMPLE_PDF") == "1"
PAGE_BREAK: Final = "=== page break ===\n"

# docs/business-context.md §7: the businesses' types and supported languages.
SECTION_7: Final = {
    "sunrise-clinic": ("human_healthcare", ("en", "hi")),
    "paws-and-co": ("veterinary", ("en", "te")),
}

# Facts whose value an answer can be traced by (zara's review, Z3): amounts in INR (in text, or a
# CSV `price_inr` cell), durations and distances, and opening-time ranges.
AMOUNT: Final = re.compile(r"(?:INR|₹)\s?(\d[\d,]*)|price_inr: (?:from )?(\d+)")
QUANTITY: Final = re.compile(r"\b(\d+) (?:working )?(days?|hours?|km)\b")
TIME_RANGE: Final = re.compile(r"\b(\d{1,2}:\d{2}) to (\d{1,2}:\d{2})\b")


def test_each_business_folder_is_listed_completely_by_its_manifest() -> None:
    for business_id in BUSINESSES:
        folder = CORPUS_DIR / business_id
        manifest, documents = read_business(folder, business_id)
        assert manifest.business_id == business_id
        assert unlisted_files(folder, manifest) == [], business_id
        assert len(documents) == len(manifest.sources) == 9


def test_the_businesses_are_section_7s_with_their_types_and_languages() -> None:
    corpus = real_corpus()
    assert corpus.business_ids == set(SECTION_7)
    for business_id, (business_type, languages) in SECTION_7.items():
        manifest = corpus.manifests[business_id]
        assert (manifest.business_type, manifest.languages) == (business_type, languages)
        assert manifest.synthetic is True
        assert manifest.default_language == "en"


@pytest.mark.parametrize("max_chars", [200, 1200, 8000])
def test_every_document_loads_and_chunks_at_any_allowed_chunk_size(max_chars: int) -> None:
    corpus = real_corpus()
    config = ChunkingConfig(max_chars=max_chars, overlap_chars=min(150, max_chars // 2 - 1))
    for business_id in BUSINESSES:
        for document in corpus.documents(business_id):
            chunks = corpus.chunks(document, config)
            assert chunks, document.source.file
            assert chunks == chunk_document(document.loaded, config)  # what ingestion stores


def test_every_format_and_every_content_kind_is_used() -> None:
    sources = [d.source for b in BUSINESSES for d in real_corpus().documents(b)]
    assert {s.format for s in sources} == set(SourceType)
    assert {s.content_kind for s in sources} == set(ContentKind)
    for source in sources:
        source.ingest_params()  # valid upload parameters (raises otherwise)


def test_each_business_has_documents_that_must_never_answer_a_customer() -> None:
    corpus = real_corpus()
    for business_id in BUSINESSES:
        audiences = Counter(d.source.audience for d in corpus.documents(business_id))
        assert audiences["staff_only"] >= 1, business_id
    sunrise = {d.source.key: d.source.status for d in corpus.documents("sunrise-clinic")}
    paws = {d.source.key: d.source.status for d in corpus.documents("paws-and-co")}
    # A retired older version next to the approved one (ADR 0010), and a draft.
    assert sunrise[("fees", "2025-10")] == "retired"
    assert sunrise[("fees", "2026-04")] == "approved"
    assert paws[("holiday-hours", "2026-12")] == "draft"


def test_both_businesses_use_the_same_source_ids_with_different_documents() -> None:
    corpus = real_corpus()
    ids = [{d.source.source_id for d in corpus.documents(b)} for b in BUSINESSES]
    shared = ids[0] & ids[1]
    assert {"faq", "services", "fees", "appointment-policy", "emergency"} <= shared
    for source_id in shared:
        texts = [
            {d.loaded.text for d in corpus.documents(b) if d.source.source_id == source_id}
            for b in BUSINESSES
        ]
        assert not texts[0] & texts[1], source_id


def test_every_document_says_in_its_own_text_that_it_is_synthetic() -> None:
    for business_id in BUSINESSES:
        for document in real_corpus().documents(business_id):
            text = document.loaded.text.casefold()
            assert "synthetic" in text or "सिंथेटिक" in text, document.source.file


def test_the_hindi_document_is_in_hindi_and_carries_shesha_s_review() -> None:
    # Decided 2026-10-22, item 1: faq-hi is `review: pending` until shesha has read it, and
    # `freeze` refuses until then (test_checks.py). `done` keeps this test green.
    corpus = real_corpus()
    hindi = [d for d in corpus.documents("sunrise-clinic") if d.source.language == "hi"]
    assert [d.source.source_id for d in hindi] == ["faq-hi"]
    assert re.search(r"[ऀ-ॿ]{20}", hindi[0].loaded.text.replace(" ", ""))
    reviewed = [d.source for b in BUSINESSES for d in corpus.documents(b) if d.source.review]
    assert [(s.source_id, s.language) for s in reviewed] == [("faq-hi", "hi")]


def _texts() -> list[tuple[str, str]]:
    texts = [
        (f"{b}/{d.source.file}", d.loaded.text)
        for b in BUSINESSES
        for d in real_corpus().documents(b)
    ]
    # HTML attributes (links) are not in the loaded text: read the raw files too.
    for path in sorted(CORPUS_DIR.glob("*/*")):
        if path.suffix in {".html", ".md", ".csv", ".txt"}:
            texts.append((str(path.relative_to(CORPUS_DIR)), path.read_text("utf-8")))
    return texts


def test_contact_details_are_fake_and_domains_are_reserved() -> None:
    # The same screen the question rows go through (personal_data.py), plus links.
    for where, text in _texts():
        assert personal_data_findings(text) == [], where
        for match in URL_HOST.finditer(text):
            assert match.group(1).endswith(RESERVED_DOMAIN), where


def _facts(document: CorpusDocument) -> set[str]:
    text = document.loaded.text
    facts = {f"INR {(m.group(1) or m.group(2)).replace(',', '')}" for m in AMOUNT.finditer(text)}
    facts |= {f"{m.group(1)} {m.group(2).rstrip('s')}" for m in QUANTITY.finditer(text)}
    facts |= {f"{m.group(1)}-{m.group(2)}" for m in TIME_RANGE.finditer(text)}
    return facts


def test_values_in_documents_that_never_answer_appear_in_no_approved_public_document() -> None:
    # An answer quoting one of these values can only have come from a retired, draft or
    # staff-only document. The intended confusables (Sunrise vs Paws & Co) are between approved
    # documents only (data/sample/README.md).
    documents = [d for b in BUSINESSES for d in real_corpus().documents(b)]
    never = [d for d in documents if not d.source.answers_customers]
    approved: set[str] = set().union(*(_facts(d) for d in documents if d.source.answers_customers))
    assert {d.source.source_id for d in never} == {
        "fees",
        "staff-handbook",
        "staff-notes",
        "holiday-hours",
    }
    for document in never:
        facts = _facts(document)
        assert facts, document.source.file  # the patterns still find this document's values
        assert not facts & approved, (document.source.file, sorted(facts & approved))


def test_emergency_guidance_lists_examples_and_ends_with_a_catch_all() -> None:
    # zara's review (Z4): a closed list invites "not on the list, so not urgent". The text is
    # customer-facing guidance, never a control: M4 and M7 decide escalation.
    corpus = real_corpus()
    for business_id in BUSINESSES:
        emergency = next(
            d for d in corpus.documents(business_id) if d.source.source_id == "emergency"
        )
        assert "for example" in emergency.loaded.text, business_id
        assert "if in doubt" in emergency.loaded.text, business_id
    hindi = next(d for d in corpus.documents("sunrise-clinic") if d.source.source_id == "faq-hi")
    assert "उदाहरण के लिए" in hindi.loaded.text
    assert "शक हो तो" in hindi.loaded.text


def test_the_sample_pdf_is_built_from_its_text_source() -> None:
    # UPDATE_SAMPLE_PDF=1 rewrites the PDF from its .pdf.txt; review the diff before committing.
    corpus = real_corpus()
    pdfs = [d for b in BUSINESSES for d in corpus.documents(b) if d.source.format == "pdf"]
    assert pdfs
    for document in pdfs:
        path = CORPUS_DIR / document.business_id / document.source.file
        source = path.with_name(path.name + PDF_TEXT_SUFFIX).read_text("utf-8")
        pages = [tuple(block.strip("\n").split("\n")) for block in source.split(PAGE_BREAK)]
        expected = build_pdf(pages, title=document.source.title)
        if UPDATE:
            path.write_bytes(expected)
        assert path.read_bytes() == expected, f"rebuild {path.name} (see the test's comment)"
        assert document.loaded.page_count == len(pages)
        assert [s.page for s in document.loaded.sections] == list(range(1, len(pages) + 1))

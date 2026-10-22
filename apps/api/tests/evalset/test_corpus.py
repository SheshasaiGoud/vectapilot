"""M2 PR 7: corpus manifests, anchors (quote → chunks) and the lookup tool, on small inputs."""

import json
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from tests.evalset.helpers import CORPUS_DIR, real_corpus, reviewed_corpus, tiny_corpus
from vectapilot_api.evalset.corpus import (
    Corpus,
    CorpusError,
    CorpusManifest,
    CorpusSource,
    KeyProblem,
    anchor_spans,
    chunks_containing,
    load_business_documents,
    load_corpus_manifest,
    lookup,
    manifest_sha256,
    normalize_anchor,
    resolve_anchor,
    review_problems,
)
from vectapilot_api.evalset.files import read_business, read_corpus, unlisted_files
from vectapilot_api.ingestion.types import Chunk, ChunkingConfig, SourceType

CANARY: Final = "canary-7f3a-not-in-errors"


def source(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "file": "faq.md",
        "format": "markdown",
        "source_id": "faq",
        "version": "1",
        "title": "FAQ",
        "content_kind": "faq",
        "language": "en",
        "status": "approved",
        "audience": "public",
    }
    data.update(overrides)
    return data


def corpus_manifest(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "business_id": "tiny-shop",
        "name": "Tiny Shop",
        "business_type": "ecommerce",
        "synthetic": True,
        "created": "2026-10-22",
        "authored_by": "tests",
        "languages": ["en"],
        "default_language": "en",
        "sources": [source()],
    }
    data.update(overrides)
    return data


# ── Manifests ─────────────────────────────────────────────────────────────────────────────────


def test_a_source_carries_exactly_the_upload_parameters() -> None:
    entry = CorpusSource.model_validate(source(review_by="2027-01-31"))
    params = entry.ingest_params()
    # `review` is shesha's review of the sample file, not something an upload declares.
    assert params.model_dump(exclude={"title"}) == entry.model_dump(
        exclude={"file", "format", "title", "review"}
    )
    assert params.title == "FAQ"
    assert entry.key == ("faq", "1")
    assert entry.answers_customers


@pytest.mark.parametrize(
    ("overrides", "answers"),
    [
        ({"status": "draft"}, False),
        ({"status": "retired"}, False),
        ({"audience": "staff_only"}, False),
        ({}, True),
    ],
)
def test_only_approved_public_sources_answer_customers(
    overrides: dict[str, str], answers: bool
) -> None:
    assert CorpusSource.model_validate(source(**overrides)).answers_customers is answers


@pytest.mark.parametrize(
    "overrides",
    [
        {"file": "faq.html"},  # the extension must match the format
        {"file": "../faq.md"},  # no directory part
        {"file": "sub/faq.md"},
        {"format": "docx"},
        {"content_kind": "pdf"},
        {"status": "published"},
        {"language": "English"},
        {"review": "maybe"},
    ],
)
def test_a_bad_source_entry_is_refused(overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        CorpusSource.model_validate(source(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"synthetic": False},
        {"default_language": "hi"},
        {"sources": [source(), source()]},  # same (source_id, version)
        {"sources": [source(), source(version="2")]},  # same file
        {"sources": [source(), source(version="2", file="faq-2.md")]},  # two approved versions
        {"sources": []},
        {"business_id": "Tiny Shop"},
        {"review": "pending"},  # review moved to each document (decided 2026-10-22, item 1)
    ],
)
def test_a_bad_corpus_manifest_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CorpusManifest.model_validate(corpus_manifest(**overrides))


def test_a_draft_or_retired_version_may_sit_next_to_the_approved_one() -> None:
    sources = [source(), source(version="0", file="faq-0.md", status="retired")]
    sources.append(source(version="2", file="faq-2.md", status="draft"))
    assert len(CorpusManifest.model_validate(corpus_manifest(sources=sources)).sources) == 3


def test_a_manifest_error_names_the_fields_never_the_values() -> None:
    text = '{"business_id": "tiny-shop", "name": "x", "title": "' + CANARY + '"}'
    with pytest.raises(CorpusError) as caught:
        load_corpus_manifest(text, where="tiny-shop/manifest.json")
    message = str(caught.value)
    assert message.startswith("tiny-shop/manifest.json is not a valid corpus manifest: ")
    assert "sources" in message
    assert CANARY not in message


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        (
            {"sources": [source(), source(version="2", file="faq-2.md")]},
            "(manifest) (Value error, at most one approved version per source (ADR 0010))",
        ),
        (
            {"default_language": "hi"},
            "(manifest) (Value error, default_language must be one of languages)",
        ),
        (
            {"synthetic": False},
            "synthetic (Value error, the sample corpus is synthetic (business-context §1, Q1))",
        ),
        (
            {"sources": [source(), source(version="2", file="faq-2.html", status="draft")]},
            "sources.1 (Value error, the file name must end with its format's extension)",
        ),
    ],
)
def test_a_manifest_error_keeps_the_rule_it_broke(overrides: dict[str, Any], rule: str) -> None:
    # Mutation audit D10: "(manifest)" alone did not say which rule. Rule texts are fixed
    # sentences; the canary title shows no value is echoed.
    text = json.dumps(corpus_manifest(**overrides) | {"name": CANARY})
    with pytest.raises(CorpusError) as caught:
        load_corpus_manifest(text, where="tiny-shop/manifest.json")
    assert str(caught.value) == f"tiny-shop/manifest.json is not a valid corpus manifest: {rule}"
    assert CANARY not in str(caught.value)


def test_review_problems_name_each_pending_document() -> None:
    sources = [
        source(),
        source(file="faq-hi.md", source_id="faq-hi", language="hi", review="pending"),
    ]
    sources.append(source(file="faq-te.md", source_id="faq-te", language="te", review="done"))
    manifest = CorpusManifest.model_validate(corpus_manifest(sources=sources))
    corpus = Corpus([manifest], [])
    assert review_problems(corpus, ["tiny-shop", "no-such-shop"]) == [
        "tiny-shop/faq-hi.md (faq-hi 1) is review: pending; shesha reviews it and sets `review` "
        "to done in its manifest entry before the freeze"
    ]
    assert review_problems(reviewed_corpus(), ["sunrise-clinic", "paws-and-co"]) == []


def test_the_manifest_hash_leaves_each_documents_review_out() -> None:
    sunrise = real_corpus().manifests["sunrise-clinic"]
    assert manifest_sha256(sunrise) == manifest_sha256(
        reviewed_corpus().manifests["sunrise-clinic"]
    )


def test_a_file_the_loader_refuses_names_the_file_and_the_error_class() -> None:
    manifest = CorpusManifest.model_validate(corpus_manifest())
    with pytest.raises(CorpusError, match=r"^tiny-shop/faq.md does not load: EmptyDocumentError$"):
        load_business_documents(manifest, lambda _name: b"   ")


# ── Reading folders ───────────────────────────────────────────────────────────────────────────


def _folder(tmp_path: Path, manifest: dict[str, Any], files: dict[str, bytes]) -> Path:
    folder: Path = tmp_path / str(manifest["business_id"])
    folder.mkdir()
    (folder / "manifest.json").write_text(CorpusManifest.model_validate(manifest).model_dump_json())
    for name, data in files.items():
        (folder / name).write_bytes(data)
    return folder


def test_reading_a_business_folder(tmp_path: Path) -> None:
    folder = _folder(tmp_path, corpus_manifest(), {"faq.md": b"# FAQ\n\nTEST-ONLY text.\n"})
    manifest, documents = read_business(folder, "tiny-shop")
    assert [d.source.file for d in documents] == ["faq.md"]
    assert unlisted_files(folder, manifest) == []
    (folder / "notes.txt").write_text("unexplained")
    (folder / "faq.md.txt").write_text("only a listed PDF may have a text source")
    assert unlisted_files(folder, manifest) == ["faq.md.txt", "notes.txt"]


def test_a_listed_pdfs_text_source_is_explained(tmp_path: Path) -> None:
    pdf = source(file="fees.pdf", format="pdf", source_id="fees")
    folder = _folder(tmp_path, corpus_manifest(sources=[pdf]), {"fees.pdf.txt": b"x"})
    manifest = CorpusManifest.model_validate(corpus_manifest(sources=[pdf]))
    assert unlisted_files(folder, manifest) == []


def test_reading_refuses_a_missing_manifest_another_business_or_a_missing_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "empty-shop").mkdir()
    with pytest.raises(CorpusError, match=r"has no manifest\.json"):
        read_business(tmp_path / "empty-shop", "empty-shop")
    folder = _folder(tmp_path, corpus_manifest(), {})
    with pytest.raises(CorpusError, match="names another business"):
        read_business(folder, "other-shop")
    with pytest.raises(CorpusError, match=r"tiny-shop/faq.md is listed but missing"):
        read_business(folder, "tiny-shop")


def test_read_corpus_skips_a_business_without_a_folder() -> None:
    corpus = read_corpus(CORPUS_DIR, ["sunrise-clinic", "no-such-business"])
    assert corpus.business_ids == {"sunrise-clinic"}
    assert corpus.documents("no-such-business") == ()


def test_file_hashes_list_every_listed_file() -> None:
    hashes = real_corpus().file_hashes()
    assert sorted(hashes) == ["paws-and-co", "sunrise-clinic"]
    assert len(hashes["sunrise-clinic"]) == 9
    assert "fees-2026-04.pdf" in hashes["sunrise-clinic"]


def test_pins_hold_the_file_hashes_and_one_manifest_hash_per_business() -> None:
    corpus = real_corpus()
    pins = corpus.pins()
    assert pins.files == corpus.file_hashes()
    assert pins.manifests == {
        b: manifest_sha256(corpus.manifests[b]) for b in ("paws-and-co", "sunrise-clinic")
    }
    assert pins.manifests["paws-and-co"] != pins.manifests["sunrise-clinic"]


# ── Anchors ───────────────────────────────────────────────────────────────────────────────────


def test_anchors_are_nfc_with_whitespace_collapsed() -> None:
    assert normalize_anchor("  free up\nto\t 24  hours ") == "free up to 24 hours"
    decomposed = "Café hours"  # e + combining acute accent
    assert normalize_anchor(decomposed) == "Café hours"


def test_anchor_spans_ignore_whitespace_differences_and_index_the_original_text() -> None:
    text = "Line one says free\nup to  24 hours. And free up to 24 hours again."
    spans = anchor_spans(text, "free up to 24 hours")
    # "Line one says " is 14 characters; "hours." ends at 35, "And " follows, "free" is at 40.
    assert spans == [(14, 34), (40, 59)]
    assert text[14:34] == "free\nup to  24 hours"
    assert text[40:59] == "free up to 24 hours"
    assert anchor_spans(text, "   ") == []
    assert anchor_spans(text, "free up to 48 hours") == []


def chunk(index: int, start: int, end: int) -> Chunk:
    return Chunk(
        source_id="doc",
        version="1",
        source_type=SourceType.MARKDOWN,
        chunk_index=index,
        content="x" * (end - start),
        char_start=start,
        char_end=end,
    )


def test_chunks_containing_needs_the_whole_span_inside_a_chunk() -> None:
    # Two overlapping chunks, [0, 100) and [80, 180), and a third, [180, 250).
    chunks = [chunk(0, 0, 100), chunk(1, 80, 180), chunk(2, 180, 250)]
    assert chunks_containing(chunks, 10, 20) == (0,)
    assert chunks_containing(chunks, 85, 95) == (0, 1)  # in the overlap: both count
    assert chunks_containing(chunks, 95, 105) == (1,)
    assert chunks_containing(chunks, 170, 190) == ()  # crosses a boundary without overlap
    assert chunks_containing(chunks, 0, 100) == (0,)  # end is exclusive


PARAGRAPH: Final = (
    "TEST-ONLY. Our store opens at nine. "
    + "Filler words keep this paragraph long enough to split. " * 8
    + "Unique closing words appear exactly here."
)


def test_resolve_anchor_finds_the_chunks_that_hold_the_quote() -> None:
    corpus = tiny_corpus(f"# Doc\n\n{PARAGRAPH}\n".encode())
    document = corpus.documents("tiny-shop")[0]
    assert resolve_anchor(corpus, document, "Our store opens at nine.") == (0,)  # default: 1 chunk
    small = ChunkingConfig(max_chars=200, overlap_chars=60)
    indexes = resolve_anchor(corpus, document, "Unique closing words appear exactly here.", small)
    last = corpus.chunks(document, small)[-1].chunk_index
    assert last > 0
    assert indexes[-1] == last


@pytest.mark.parametrize(
    ("anchor", "config", "problem"),
    [
        ("Our store closes at ten.", ChunkingConfig(), "is not in the document's loaded text"),
        # Mutation audit M3a: a quote that differs only in case is not the document's text.
        ("our store opens at nine.", ChunkingConfig(), "is not in the document's loaded text"),
        ("Filler words keep", ChunkingConfig(), "occurs 8 times in the document; quote more"),
        (
            # Longer than the overlap, so at max_chars=200 no single chunk holds it.
            "Our store opens at nine. Filler words keep this paragraph long enough to split. "
            "Filler words keep this paragraph long enough to split. Filler words keep this",
            ChunkingConfig(max_chars=200, overlap_chars=0),
            "no chunk holds the whole anchor at this chunk size",
        ),
    ],
)
def test_an_anchor_that_does_not_resolve_says_why(
    anchor: str, config: ChunkingConfig, problem: str
) -> None:
    corpus = tiny_corpus(f"# Doc\n\n{PARAGRAPH}\n".encode())
    with pytest.raises(KeyProblem, match=problem) as caught:
        resolve_anchor(corpus, corpus.documents("tiny-shop")[0], anchor, config)
    assert "store" not in str(caught.value)  # fixed text, never the quote


def test_an_anchor_across_two_sections_never_resolves() -> None:
    corpus = tiny_corpus(b"# Doc\n\n## A\n\nFirst part ends here.\n\n## B\n\nSecond part.\n")
    with pytest.raises(KeyProblem, match="no chunk holds the whole anchor"):
        resolve_anchor(corpus, corpus.documents("tiny-shop")[0], "ends here. ## B")


# ── Lookup ────────────────────────────────────────────────────────────────────────────────────


def test_lookup_finds_lines_in_every_document_whatever_its_status() -> None:
    lines = lookup(real_corpus(), "sunrise-clinic", "CANCEL  at least")
    found = {(line.source.source_id, line.source.version, line.source.status) for line in lines}
    assert found == {("fees", "2026-04", "approved"), ("fees", "2025-10", "retired")}
    retired = next(line for line in lines if line.source.status == "retired")
    assert retired.heading_path[-1] == "Refunds"
    assert retired.chunk_indexes  # a line inside one chunk
    pdf = next(line for line in lines if line.source.format == "pdf")
    assert (pdf.page, pdf.row) == (2, None)


def test_lookup_reports_csv_rows_and_stays_inside_one_business() -> None:
    lines = lookup(real_corpus(), "paws-and-co", "consultation (dog or cat)")
    assert [(line.source.source_id, line.row) for line in lines] == [("fees", 1)]
    assert lookup(real_corpus(), "sunrise-clinic", "consultation (dog or cat)") == ()
    assert lookup(real_corpus(), "paws-and-co", "  ") == ()
    assert lookup(real_corpus(), "no-such-business", "fees") == ()

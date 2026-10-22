"""Shared fixtures for the evalset tests: the real sample corpus, and made-up rows.

Every row built here is a placeholder on purpose (an `example-` id and `TEST-ONLY` text), so the
real dataset's check refuses it; tests that need a row to pass call the checks with
`allow_placeholders=True`. No row here is an eval question.
"""

import functools
import shutil
from pathlib import Path
from typing import Any, Final

from vectapilot_api.evalset.corpus import (
    Corpus,
    CorpusDocument,
    CorpusManifest,
    CorpusSource,
    load_business_documents,
)
from vectapilot_api.evalset.files import read_corpus
from vectapilot_api.evalset.questions import EvalManifest, EvalQuestion

REPO_ROOT: Final = Path(__file__).parents[4]
CORPUS_DIR: Final = REPO_ROOT / "data" / "sample"
DATASET_DIR: Final = REPO_ROOT / "evals" / "datasets" / "support_qa" / "v0.1"
BUSINESSES: Final = ("sunrise-clinic", "paws-and-co")

# Quotes from the real sample documents, used as anchors by the fixtures below.
SUNRISE_HOURS: Final = {
    "source_id": "faq",
    "version": "2026-10",
    "anchor": "Saturday: 9:00 to 14:00",
}
SUNRISE_FEE: Final = {
    "source_id": "fees",
    "version": "2026-04",
    "anchor": "General consultation (general physician): INR 600",
}
PAWS_CANCEL: Final = {
    "source_id": "appointment-policy",
    "version": "2026-09",
    "anchor": "free up to 12 hours before it starts",
}
PAWS_DEPOSIT: Final = {
    "source_id": "appointment-policy",
    "version": "2026-09",
    "anchor": "need a deposit of INR 1,000 when you book",
}


@functools.cache
def real_corpus() -> Corpus:
    """The committed corpus, `faq-hi` still `review: pending` (decided 2026-10-22, item 1)."""
    return read_corpus(CORPUS_DIR, BUSINESSES)


@functools.cache
def reviewed_corpus() -> Corpus:
    """The committed corpus as it will be once shesha has reviewed every pending document: what a
    test that freezes needs (`freeze` refuses while a review is pending)."""
    corpus = real_corpus()

    def done(source: CorpusSource) -> CorpusSource:
        return source.model_copy(update={"review": "done"}) if source.review else source

    manifests = [
        m.model_copy(update={"sources": tuple(done(s) for s in m.sources)})
        for m in corpus.manifests.values()
    ]
    documents = [
        CorpusDocument(d.business_id, done(d.source), d.loaded, d.file_sha256)
        for b in corpus.business_ids
        for d in corpus.documents(b)
    ]
    return Corpus(manifests, documents)


def copy_reviewed_corpus(destination: Path) -> Path:
    """Copy data/sample to `destination` with every pending review marked done (`reviewed_corpus`
    on disk); returns `destination`."""
    shutil.copytree(CORPUS_DIR, destination)
    for path in destination.glob("*/manifest.json"):
        text = path.read_text("utf-8")
        path.write_text(text.replace('"review": "pending"', '"review": "done"'), "utf-8")
    return destination


def manifest_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "dataset": "support_qa",
        "version": "v0.1",
        "created": "2026-10-22",
        "synthetic": True,
        "authored_by": "tests",
        "stage": "collecting",
        "corpus": "data/sample",
        "businesses": list(BUSINESSES),
        "split_rule": {
            "rule": "stratified-systematic-v2",
            "seed": "support_qa",
            "test_percent": 30,
        },
        "requirements": {
            "min_questions": 40,
            "min_hindi": 10,
            "min_friend_blind": 10,
            "min_per_category": 3,
            "min_per_business": 15,
            "min_safety_per_business": 3,
            "min_no_answer": 8,
        },
        "previous_version": None,
    }
    data.update(overrides)
    return data


def manifest(**overrides: Any) -> EvalManifest:
    return EvalManifest.model_validate(manifest_data(**overrides))


SMALL_REQUIREMENTS: Final = {
    "min_questions": 1,
    "min_hindi": 0,
    "min_friend_blind": 0,
    "min_per_category": 1,
    "min_per_business": 1,
    "min_safety_per_business": 0,
    "min_no_answer": 0,
}


def row(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "example-1",
        "business_id": "sunrise-clinic",
        "question": "TEST-ONLY question 1",
        "language": "en",
        "category": "faq",
        "author_type": "team",
        "saw_documents": True,
        "expected_outcome": "answered",
        "expected_evidence": [[SUNRISE_HOURS]],
        "created": "2026-10-22",
        "synthetic": True,
    }
    data.update(overrides)
    return data


def question(**overrides: Any) -> EvalQuestion:
    return EvalQuestion.model_validate(row(**overrides))


def one_per_category() -> list[EvalQuestion]:
    """One valid placeholder row for each category v0.1 accepts, alternating businesses."""
    rows = [
        row(id="example-faq", category="faq"),
        row(
            id="example-policy",
            category="policy",
            business_id="paws-and-co",
            expected_evidence=[[PAWS_CANCEL]],
        ),
        row(
            id="example-multi",
            category="multi_hop",
            expected_evidence=[[SUNRISE_HOURS], [SUNRISE_FEE]],
        ),
        row(
            id="example-ambiguous",
            category="ambiguous",
            business_id="paws-and-co",
            expected_evidence=[[PAWS_CANCEL], [PAWS_DEPOSIT]],  # one group per reading
        ),
        row(
            id="example-oos",
            category="out_of_scope",
            expected_outcome="dont_know",
            expected_evidence=[],
        ),
        row(
            id="example-adversarial",
            category="adversarial",
            business_id="paws-and-co",
            expected_outcome="dont_know",
            expected_evidence=[],
        ),
        row(
            id="example-safety",
            category="safety",
            expected_outcome="handoff",
            expected_evidence=[],
        ),
        row(
            id="example-handoff",
            category="handoff_request",
            business_id="paws-and-co",
            expected_outcome="handoff",
            expected_evidence=[],
        ),
    ]
    return [EvalQuestion.model_validate({**r, "question": f"TEST-ONLY {r['id']}"}) for r in rows]


def tiny_corpus(text: bytes, *, business_id: str = "tiny-shop", **source: Any) -> Corpus:
    """A one-document corpus from Markdown bytes, for anchor edge cases."""
    entry: dict[str, Any] = {
        "file": "doc.md",
        "format": "markdown",
        "source_id": "doc",
        "version": "1",
        "title": "Doc",
        "content_kind": "faq",
        "language": "en",
        "status": "approved",
        "audience": "public",
    }
    entry.update(source)
    corpus_manifest = CorpusManifest.model_validate(
        {
            "business_id": business_id,
            "name": "Tiny Shop (TEST-ONLY)",
            "business_type": "ecommerce",
            "synthetic": True,
            "created": "2026-10-22",
            "authored_by": "tests",
            "languages": ["en"],
            "default_language": "en",
            "sources": [entry],
        }
    )
    documents: tuple[CorpusDocument, ...] = load_business_documents(
        corpus_manifest, lambda _name: text
    )
    return Corpus([corpus_manifest], documents)


GOLDEN_LAYOUT: Final = (("adversarial", 2), ("faq", 2), ("out_of_scope", 2), ("policy", 4))
"""test_split.py's golden case: one author, language and business, so the line is these runs in
name order. Two categories miss test and compete for the one spare test row."""


def golden_layout() -> list[EvalQuestion]:
    """The `GOLDEN_LAYOUT` rows: TEST-ONLY attributes, no evidence (`dont_know`)."""
    return [
        question(
            id=f"example-{category.replace('_', '-')}-{k}",
            question=f"TEST-ONLY {category} {k}",
            category=category,
            expected_outcome="dont_know",
            expected_evidence=[],
        )
        for category, count in GOLDEN_LAYOUT
        for k in range(count)
    ]

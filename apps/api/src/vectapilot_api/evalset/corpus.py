"""The sample corpus (M2 PR 7): fictional businesses' documents under `data/sample/<business_id>/`.

Each business folder has a `manifest.json` (`CorpusManifest`) listing its documents with exactly
the upload metadata of `POST /v1/kb/documents` (`IngestParams`) plus the file and its format. The
documents are loaded and chunked with the real ingestion code, so what the eval resolves is what
ingestion stores.

**How an eval question points at its evidence (a `SourceKey`).** Not `chunks.id`: it is a random
UUID, new on every ingest. Not `(source_id, version, chunk_index)` either: the chunk numbers move
whenever the chunk size changes, and nothing records the chunk size the stored chunks were made
with (re-ingesting an existing `(source_id, version)` does not re-chunk). A key is
`(source_id, version, anchor)`: the document by the identity the business gives it (unique per
business, `UNIQUE (business_id, source_id, version)`), and a short verbatim quote from its loaded
text. The business is never part of the key: a key always resolves inside the question's own
business, so the same `fees` key cannot reach the other business's `fees`.

**When a retrieved hit counts as relevant** is by content, not by chunk number (`relevance.py`,
the rule PR 8 scores with). `resolve_key` stays the check when a label is written: it finds the
quote exactly once (whitespace-insensitive, case-sensitive) and lists the chunks that hold all of
it at a given chunking config. At a config, a group none of whose keys any chunk holds makes its
question unscorable there; a single key no chunk holds, in a group another key still finds, is
counted in the report as an unheld key. Neither is ever dropped (PR 8; the per-group rule was
decided by shesha on 2026-10-22, ADR 0013, with a conservative line counting unscorable questions
as misses, and chunk configs compared on dev only over questions scorable under both).

**Review before the freeze (decided 2026-10-22, item 1).** A document Claude wrote in a language
shesha must check (`faq-hi`) carries `review: pending` in its manifest entry until shesha has read
it; `review_problems` lists those, and `freeze` refuses while any is pending.

Pure functions over bytes and values; `files.py` does the reading.
"""

import hashlib
import json
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.ingestion.chunking import DEFAULT_CHUNKING, chunk_document
from vectapilot_api.ingestion.loaders import load_document
from vectapilot_api.ingestion.types import (
    Chunk,
    ChunkingConfig,
    IngestionError,
    LoadedDocument,
    SourceType,
)
from vectapilot_api.kb.types import Audience, ContentKind, DocumentStatus, IngestParams

CORPUS_MANIFEST_FILE: Final = "manifest.json"

EXTENSIONS: Final[Mapping[SourceType, str]] = {
    SourceType.MARKDOWN: ".md",
    SourceType.HTML: ".html",
    SourceType.CSV: ".csv",
    SourceType.PDF: ".pdf",
}
"""The file name ending each format must have, so a reader of the folder sees the format."""

PDF_TEXT_SUFFIX: Final = ".txt"
"""`<name>.pdf.txt` is the text a sample PDF is built from (README); it is not a source itself."""

ANCHOR_MIN_CHARS: Final = 8
ANCHOR_MAX_CHARS: Final = 100
"""An anchor is a short quote: long enough to be unique in its document, short enough to fit in
one chunk at most chunk sizes. No length *guarantees* that: chunks overlap by `overlap_chars`, so
only a quote no longer than the overlap always lies inside one chunk; a longer one can straddle
a boundary at a small chunk size (ananya's review, F5, measured windows of 80-200 characters
straddling at 200/40 and 400/50). So labellers quote only the clause that states the fact,
`check --max-chars/--overlap` resolves every key at another config, and a key that straddles at
a config is unscorable there, never dropped (PR 8)."""

FileName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$")]
"""A bare file name in the business folder: no directory part, so no path can leave the folder."""

LanguageTag = Annotated[str, Field(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")]
"""A BCP 47 tag, as `SourceMeta.language` accepts it."""

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

MANIFEST_HASHED_FIELDS: Final = frozenset(
    {"business_id", "business_type", "languages", "default_language", "sources"}
)
"""What a freeze pins of a corpus manifest: which documents exist and may answer whom, in which
languages. Left out: `name`, `created`, `authored_by`, `synthetic` (always true) and each
source's `review` (a frozen version has none pending: `review_problems`)."""

ReviewState = Literal["pending", "done"]


class CorpusError(ValueError):
    """The corpus cannot be used. The message names the business, file or source and the rule,
    never document text."""


def describe_validation_error(error: ValidationError, whole: str) -> str:
    """The failing fields of a file model (`whole` for the model itself), then the rule text of
    every rule our own validators raise (`value_error`: fixed sentences, never a value). Pydantic's
    other messages stay out: the fields say where, and `hide_input_in_errors` keeps values out."""
    errors = error.errors()
    fields = sorted({".".join(map(str, e["loc"])) or whole for e in errors})
    rules = sorted({e["msg"] for e in errors if e["type"] == "value_error"})
    return ", ".join(fields) + (f" ({'; '.join(rules)})" if rules else "")


class EvalModel(BaseModel):
    """Base of the evalset's file models: immutable, closed, and values kept out of errors."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class CorpusSource(EvalModel):
    """One document of a business: its file and exactly what an upload declares about it."""

    file: FileName
    format: SourceType
    source_id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    content_kind: ContentKind
    language: LanguageTag
    status: DocumentStatus
    audience: Audience
    review_by: date | None = None
    review: ReviewState | None = None
    """shesha's review of a document Claude wrote in a language shesha must check (decided
    2026-10-22, item 1): `pending` until shesha has read it, then `done`. Absent (None) when no
    separate review is asked for. Not an upload parameter."""

    @model_validator(mode="after")
    def _extension_matches_format(self) -> Self:
        if not self.file.endswith(EXTENSIONS[self.format]):
            raise ValueError("the file name must end with its format's extension")
        return self

    @property
    def key(self) -> tuple[str, str]:
        """`(source_id, version)`: unique per business, like the database's key."""
        return (self.source_id, self.version)

    @property
    def answers_customers(self) -> bool:
        """Approved and public: the only documents a customer's answer may come from (§3.4)."""
        return self.status == "approved" and self.audience == "public"

    def ingest_params(self) -> IngestParams:
        """The query parameters of `POST /v1/kb/documents` for this file."""
        return IngestParams(
            source_id=self.source_id,
            version=self.version,
            language=self.language,
            title=self.title,
            content_kind=self.content_kind,
            status=self.status,
            audience=self.audience,
            review_by=self.review_by,
        )


class CorpusManifest(EvalModel):
    """`data/sample/<business_id>/manifest.json`."""

    business_id: BusinessId
    name: str = Field(min_length=1, max_length=200)
    business_type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    """The business-type template (business-context §2, §7), e.g. `human_healthcare`."""
    synthetic: bool
    created: date
    authored_by: str = Field(min_length=1, max_length=200)
    languages: tuple[LanguageTag, ...] = Field(min_length=1)
    """The business's supported languages (business-context §3.3, §7)."""
    default_language: LanguageTag
    sources: tuple[CorpusSource, ...] = Field(min_length=1)

    @field_validator("synthetic")
    @classmethod
    def _must_be_synthetic(cls, synthetic: bool) -> bool:
        if not synthetic:
            raise ValueError("the sample corpus is synthetic (business-context §1, Q1)")
        return synthetic

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.default_language not in self.languages:
            raise ValueError("default_language must be one of languages")
        if len({s.key for s in self.sources}) != len(self.sources):
            raise ValueError("each (source_id, version) is listed once")
        if len({s.file for s in self.sources}) != len(self.sources):
            raise ValueError("each file is listed once")
        approved = [s.source_id for s in self.sources if s.status == "approved"]
        if len(set(approved)) != len(approved):
            raise ValueError("at most one approved version per source (ADR 0010)")
        return self


def manifest_sha256(manifest: CorpusManifest) -> str:
    """SHA-256 of the manifest's `MANIFEST_HASHED_FIELDS` as canonical JSON: a status, audience,
    version, language or content-kind change moves it; reformatting the file does not."""
    payload = manifest.model_dump(
        mode="json",
        include=set(MANIFEST_HASHED_FIELDS),
        exclude={"sources": {"__all__": {"review"}}},
    )
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_corpus_manifest(text: str, *, where: str) -> CorpusManifest:
    """Parse and validate one business's manifest. Raises `CorpusError` naming `where`, the
    fields that failed and the rule a manifest-wide check broke (never a value; mutation audit
    D10: "(manifest)" alone did not say which rule)."""
    try:
        return CorpusManifest.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error, "(manifest)")
    raise CorpusError(f"{where} is not a valid corpus manifest: {detail}")


@dataclass(frozen=True)
class CorpusPins:
    """What a freeze pins of the corpus: each business's listed files and their SHA-256, and
    each business's manifest hash (kenji's review, K4)."""

    files: Mapping[str, Mapping[str, str]]
    manifests: Mapping[str, str]


@dataclass(frozen=True)
class CorpusDocument:
    """One listed document, loaded by the real loader."""

    business_id: str
    source: CorpusSource
    loaded: LoadedDocument
    file_sha256: str


def load_business_documents(
    manifest: CorpusManifest, read_file: Callable[[str], bytes]
) -> tuple[CorpusDocument, ...]:
    """Load every listed file with the ingestion loader its format names (the default limits,
    as the upload endpoint uses). Raises `CorpusError` naming the file and the loader's error
    class (its message is safe, but stays out for brevity)."""
    documents: list[CorpusDocument] = []
    for source in manifest.sources:
        data = read_file(source.file)
        params = source.ingest_params()
        try:
            loaded = load_document(data, source.format, params.source_meta())
        except IngestionError as error:
            problem = type(error).__name__
            raise CorpusError(
                f"{manifest.business_id}/{source.file} does not load: {problem}"
            ) from None
        documents.append(
            CorpusDocument(
                business_id=manifest.business_id,
                source=source,
                loaded=loaded,
                file_sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    return tuple(documents)


class Corpus:
    """The loaded documents of one or more businesses, with chunks cached per chunking config."""

    def __init__(
        self,
        manifests: Iterable[CorpusManifest],
        documents: Iterable[CorpusDocument],
    ) -> None:
        self.manifests: dict[str, CorpusManifest] = {m.business_id: m for m in manifests}
        self._documents: dict[str, dict[tuple[str, str], CorpusDocument]] = {
            business_id: {} for business_id in self.manifests
        }
        for document in documents:
            self._documents[document.business_id][document.source.key] = document
        self._chunks: dict[tuple[str, str, str, ChunkingConfig], tuple[Chunk, ...]] = {}

    @property
    def business_ids(self) -> frozenset[str]:
        return frozenset(self.manifests)

    def documents(self, business_id: str) -> tuple[CorpusDocument, ...]:
        """The business's documents in manifest order (empty for an unknown business)."""
        return tuple(self._documents.get(business_id, {}).values())

    def document(self, business_id: str, source_id: str, version: str) -> CorpusDocument | None:
        return self._documents.get(business_id, {}).get((source_id, version))

    def chunks(self, document: CorpusDocument, config: ChunkingConfig) -> tuple[Chunk, ...]:
        """The document's chunks under `config`, exactly as ingestion would store them."""
        cache_key = (document.business_id, *document.source.key, config)
        if cache_key not in self._chunks:
            self._chunks[cache_key] = chunk_document(document.loaded, config)
        return self._chunks[cache_key]

    def file_hashes(self) -> dict[str, dict[str, str]]:
        """Each business's listed files and their SHA-256 (what a freeze pins)."""
        return {
            business_id: {d.source.file: d.file_sha256 for d in self.documents(business_id)}
            for business_id in sorted(self.manifests)
        }

    def pins(self) -> CorpusPins:
        """Everything a freeze pins: the file hashes and each business's manifest hash."""
        return CorpusPins(
            files=self.file_hashes(),
            manifests={b: manifest_sha256(m) for b, m in sorted(self.manifests.items())},
        )


def review_problems(corpus: Corpus, business_ids: Iterable[str]) -> list[str]:
    """Every listed document of these businesses still waiting for shesha's review, in manifest
    order. Each one stops `freeze` (decided 2026-10-22, item 1)."""
    return [
        f"{business_id}/{source.file} ({source.source_id} {source.version}) is review: pending; "
        "shesha reviews it and sets `review` to done in its manifest entry before the freeze"
        for business_id in business_ids
        if business_id in corpus.manifests
        for source in corpus.manifests[business_id].sources
        if source.review == "pending"
    ]


# ── Anchors: from a quote to the chunks that contain it ───────────────────────────────────────


def normalize_anchor(anchor: str) -> str:
    """NFC (as the loaders normalise documents) and whitespace runs collapsed to one space. Case
    is kept: a quote that differs only in case is not the document's text."""
    return " ".join(unicodedata.normalize("NFC", anchor).split())


def _collapsed(text: str) -> tuple[str, list[int]]:
    """`text` with each whitespace run as one space, and each kept character's original index."""
    out: list[str] = []
    positions: list[int] = []
    in_space = False
    for index, char in enumerate(text):
        if char.isspace():
            if not in_space and out:
                out.append(" ")
                positions.append(index)
            in_space = True
        else:
            out.append(char)
            positions.append(index)
            in_space = False
    return "".join(out), positions


def anchor_spans(text: str, anchor: str) -> list[tuple[int, int]]:
    """Every place `anchor` occurs in `text`, ignoring differences in whitespace (a line break in
    the document matches a space in the anchor). Spans index the original `text`."""
    needle = normalize_anchor(anchor)
    if not needle:
        return []
    haystack, positions = _collapsed(text)
    spans: list[tuple[int, int]] = []
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        spans.append((positions[start], positions[end - 1] + 1))
        start = haystack.find(needle, start + 1)
    return spans


def chunks_containing(chunks: Iterable[Chunk], start: int, end: int) -> tuple[int, ...]:
    """The indexes of the chunks whose text holds all of `[start, end)` of the document."""
    return tuple(c.chunk_index for c in chunks if c.char_start <= start and end <= c.char_end)


class KeyProblem(Exception):
    """A key that does not resolve. The message is fixed text, never document or anchor text."""


def resolve_anchor(
    corpus: Corpus,
    document: CorpusDocument,
    anchor: str,
    config: ChunkingConfig = DEFAULT_CHUNKING,
) -> tuple[int, ...]:
    """The chunk indexes holding the anchor's one occurrence. Raises `KeyProblem`."""
    spans = anchor_spans(document.loaded.text, anchor)
    if not spans:
        raise KeyProblem("its anchor is not in the document's loaded text (use the lookup tool)")
    if len(spans) > 1:
        raise KeyProblem(f"its anchor occurs {len(spans)} times in the document; quote more")
    indexes = chunks_containing(corpus.chunks(document, config), *spans[0])
    if not indexes:
        raise KeyProblem(
            "no chunk holds the whole anchor at this chunk size (it crosses a chunk or section "
            "boundary); quote less"
        )
    return indexes


# ── Lookup: a human finds candidate anchors ───────────────────────────────────────────────────


@dataclass(frozen=True)
class LookupLine:
    """One line of a document that contains the search text, and where it would be cited."""

    source: CorpusSource
    heading_path: tuple[str, ...]
    page: int | None
    row: int | None
    line: str
    chunk_indexes: tuple[int, ...]


def lookup(
    corpus: Corpus,
    business_id: str,
    text: str,
    config: ChunkingConfig = DEFAULT_CHUNKING,
) -> tuple[LookupLine, ...]:
    """Every line of the business's documents (all statuses and audiences, so a human sees what
    must *not* answer too) containing `text`, case- and whitespace-insensitively."""
    needle = normalize_anchor(text).casefold()
    if not needle:
        return ()
    found: list[LookupLine] = []
    for document in corpus.documents(business_id):
        chunks = corpus.chunks(document, config)
        offset = 0
        for line in document.loaded.text.split("\n"):
            start, end = offset, offset + len(line)
            offset = end + 1
            if needle not in normalize_anchor(line).casefold():
                continue
            # Every non-empty line lies inside one section (sections are joined by a blank line).
            section = next(s for s in document.loaded.sections if s.start <= start <= s.end)
            found.append(
                LookupLine(
                    source=document.source,
                    heading_path=section.heading_path,
                    page=section.page,
                    row=section.row,
                    line=line.strip(),
                    chunk_indexes=chunks_containing(chunks, start, end),
                )
            )
    return tuple(found)

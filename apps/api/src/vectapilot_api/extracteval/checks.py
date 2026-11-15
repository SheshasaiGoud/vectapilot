"""One stage-aware check of a `doc_extraction` version, D8's minimums, and the freeze (M6 PR 4).

How CI stays green while the version is empty, and becomes strict (as `tool_calls`):
- **Always:** the slots themselves (fixed before any document: each business's slots split 50/50,
  every business-and-tag minimum with a dev and a test slot, enough slots for D8's minimums); every
  business has an extraction business file; and every document, on its own and against its slot:
  - **On its slot:** its id is a slot's, with the slot's business and split, carrying the slot's
    tags (it may carry more);
  - **Its gold row:** a kind the business enables; exactly the template's fields; dates ISO,
    enums the business's ids, identifiers within the template's pattern and length, text within
    its length; `absent` exactly when a required field is null;
  - **Its files:** a `.pdf.txt` in the format (`source.py`) and its `.pdf`; the PDF read by the
    core's own reader (`extraction.text.read_pages`) is within E12 and readable, has the
    source's pages and image pages, holds every source line's text and nothing else (a stale
    or padded PDF fails, even outside the repository; the CI test also rebuilds the committed
    ones byte for byte), and its own text passes the personal-data screen (M6 PR 4 review,
    zara-safety: a live run sends the PDF's text, not the source's); `image_page` exactly when
    a page has an image, `table` exactly when a line has a column (`x`);
  - **Content:** no placeholder marker (`EXAMPLE-ONLY`, an `example` id, a gold value with the
    word `example`: the README's illustrations are refused), no personal data
    (`evalset/personal_data.py`), on every line and every gold value;
  - **Gold is on the page** (`presence_problems`; M6 PR 4 review, ananya-rag): each text and
    identifier gold value is on a line a person sees (not `hidden`), whitespace collapsed and
    case-folded, and each enum gold's id or one of the business's synonyms for it is a whole
    token of such a line. Dates are not checked (a date printed in a format the business does not
    list still has its real ISO gold). A typo or a value copied from a hidden line would
    otherwise be frozen and turn a correct answer into a silent error.
  Files without a gold row, and stray files, are problems. D8's minimums are not checked while
  `stage: collecting`: zero documents pass, and `report` shows how far each one is.
- **`stage: frozen`:** `freeze` sets it, only when every slot is filled and the minimums hold;
  from then on the same CI test enforces them and every pin (`freeze.py`: documents, gold rows
  and slots; not the extraction configuration, whose hashes are in each run's fingerprint, while
  every gold row is still checked against the current template and business file).

A problem names the file, document id, field, page and rule, never a value or a line's text
(scoring and the README need the text; `check` never prints it). `check` never edits anything.
"""

import hashlib
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

from vectapilot_api.evalset.checks import CheckResult, FreezeRefused
from vectapilot_api.evalset.personal_data import personal_data_findings
from vectapilot_api.evalset.questions import PLACEHOLDER_MARKER, is_placeholder_text
from vectapilot_api.extracteval.freeze import (
    DocExtractionFreeze,
    DocumentPins,
    build_freeze,
    freeze_problems,
)
from vectapilot_api.extracteval.gold import (
    DOCUMENTS_DIR,
    FREEZE_FILE,
    GOLD_FILE,
    TAGS,
    DocExtractionManifest,
    GoldRow,
    repeated_ids,
)
from vectapilot_api.extracteval.runinfo import BusinessPins
from vectapilot_api.extracteval.source import (
    PDF_SUFFIX,
    SOURCE_SUFFIX,
    SourceDocument,
    SourceError,
    parse_source,
)
from vectapilot_api.extraction.config import FieldSpec, FieldType, KindConfig
from vectapilot_api.extraction.files import ExtractionConfig
from vectapilot_api.extraction.text import (
    DEFAULT_LIMITS,
    DocumentRefused,
    DocumentText,
    read_pages,
)
from vectapilot_api.intenteval.checks import Minimum

PLACEHOLDER_ID_PREFIX: Final = "example"
PLACEHOLDER_WORD: Final = re.compile(r"(?<![^\W_])example(?![^\W_])", re.IGNORECASE)
"""The whole word `example`, any case: every name in the README's illustrations has it."""
ISO_DATE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class DocumentFiles:
    """One document's files as read from `documents/` (None: missing)."""

    source: bytes | None
    pdf: bytes | None

    @property
    def pins(self) -> DocumentPins:
        return DocumentPins(
            source_sha256=None if self.source is None else hashlib.sha256(self.source).hexdigest(),
            pdf_sha256=None if self.pdf is None else hashlib.sha256(self.pdf).hexdigest(),
        )


@dataclass(frozen=True)
class DocExtractionDataset:
    """One version as read from disk (`files.read_dataset`); pure values."""

    manifest: DocExtractionManifest
    rows: tuple[GoldRow, ...]
    documents: Mapping[str, DocumentFiles]
    """Every id with a `.pdf.txt` or a `.pdf` in `documents/`."""
    stray: tuple[str, ...]
    """Other file names in `documents/` (dot files are ignored)."""
    extraction: ExtractionConfig
    freeze: DocExtractionFreeze | None

    def kind_config(self, row: GoldRow) -> KindConfig | None:
        return self.extraction.kind_config(row.business_id, row.kind)

    def business_pins(self) -> dict[str, BusinessPins]:
        """Each listed business's extraction files (`business_pins`)."""
        return business_pins(self.manifest.businesses, self.extraction)

    def pins(self) -> dict[str, DocumentPins]:
        return {i: files.pins for i, files in self.documents.items()}


def business_pins(
    businesses: tuple[str, ...], extraction: ExtractionConfig
) -> dict[str, BusinessPins]:
    """Each business's extraction files: its business file and each enabled kind's template
    (SHA-256). A business without a file or an enabled kind is left out (`business_problems`
    reports it)."""
    pins: dict[str, BusinessPins] = {}
    for business_id in businesses:
        loaded = extraction.businesses.get(business_id)
        if loaded is None or not loaded.kinds:
            continue
        pins[business_id] = BusinessPins(
            business_sha256=loaded.sha256,
            templates={kind: c.compiled.sha256 for kind, c in sorted(loaded.kinds.items())},
        )
    return pins


# ── The slots ─────────────────────────────────────────────────────────────────────────────────


def slot_problems(manifest: DocExtractionManifest) -> list[str]:
    """Whether the slots, fixed before any document, can meet D8 (module docstring)."""
    need = manifest.requirements
    problems: list[str] = []
    if len(manifest.slots) < need.min_documents:
        problems.append(f"the manifest has {len(manifest.slots)} slots, fewer than D8's documents")
    for business_id in manifest.businesses:
        mine = [s for s in manifest.slots if s.business_id == business_id]
        where = f"slots of business {business_id}"
        if len(mine) < need.min_per_business:
            problems.append(f"{where}: {len(mine)}, fewer than {need.min_per_business}")
        splits = Counter(s.split for s in mine)
        if splits["dev"] != splits["test"]:
            problems.append(f"{where}: {splits['dev']} dev and {splits['test']} test, not 50/50")
        for tag in TAGS:
            minimum = need.min_tag_per_business.get(tag, 0)
            tagged = [s for s in mine if tag in s.tags]
            if len(tagged) < minimum:
                problems.append(f"{where}: {len(tagged)} require {tag}, fewer than {minimum}")
            if minimum and {s.split for s in tagged} != {"dev", "test"}:
                problems.append(f"{where}: {tag} needs a dev and a test slot")
    return problems


def business_problems(dataset: DocExtractionDataset) -> list[str]:
    """A listed business without an extraction business file or an enabled kind."""
    problems: list[str] = []
    for business_id in dataset.manifest.businesses:
        loaded = dataset.extraction.businesses.get(business_id)
        if loaded is None or not loaded.kinds:
            problems.append(
                f"business {business_id} has no extraction business file with an enabled kind"
                f" under {dataset.manifest.extraction}"
            )
    return problems


# ── Gold rows ─────────────────────────────────────────────────────────────────────────────────


def _value_problem(spec: FieldSpec, value: str, config: KindConfig) -> str | None:
    """Why `value` is not a canonical value of `spec` (fixed text, never the value). A text
    value needs a letter or a digit: the comparison trims everything else at its ends
    (`compare.trim`), so a value of punctuation alone would equal any other."""
    if spec.type is FieldType.DATE:
        if ISO_DATE.fullmatch(value) is None:
            return "is not an ISO date (YYYY-MM-DD)"
        try:
            date.fromisoformat(value)
        except ValueError:
            return "is not a real date"
        return None
    if spec.type is FieldType.ENUM:
        allowed = {v.id for v in config.enum_values.get(spec.name, ())}
        return None if value in allowed else "is not one of the business's enum ids"
    if not any(character.isalnum() for character in value):
        return "has no letter or digit"
    limit = spec.max_length
    if limit is not None and len(value) > limit:
        return f"is longer than the template's {limit} characters"
    if spec.type is FieldType.IDENTIFIER and (
        spec.pattern is None or re.fullmatch(spec.pattern, value) is None
    ):
        return "does not match the template's identifier pattern"
    return None


def gold_problems(
    row: GoldRow, config: KindConfig, *, allow_placeholders: bool = False
) -> list[str]:
    """The row's fields against its template, and `absent` against its nulls."""
    where = f"{GOLD_FILE} document {row.id}"
    template = config.template
    names = [spec.name for spec in template.fields]
    problems = [
        f"{where}: field {name} is missing (write null when the document does not give it)"
        for name in names
        if name not in row.fields
    ]
    problems += [
        f"{where}: field {name} is not in the {row.kind} template"
        for name in sorted(set(row.fields) - set(names))
    ]
    for spec in template.fields:
        value = row.fields.get(spec.name)
        problem = None if value is None else _value_problem(spec, value, config)
        if problem is not None:
            problems.append(f"{where}: field {spec.name} {problem}")
        if value is not None and personal_data_findings(value):
            problems.append(f"{where}: field {spec.name} looks like personal data; invent it")
        if value is not None and not allow_placeholders and PLACEHOLDER_WORD.search(value):
            problems.append(
                f"{where}: field {spec.name} holds the word 'example' (the README's"
                " illustrations); invent a value"
            )
    required_null = [
        s.name for s in template.fields if s.required and row.fields.get(s.name) is None
    ]
    if required_null and "absent" not in row.tags:
        problems.append(
            f"{where}: required field(s) {', '.join(required_null)} are null, so it is tagged"
            " absent"
        )
    if "absent" in row.tags and not required_null:
        problems.append(f"{where}: tagged absent, but every required field has a gold value")
    return problems


def _is_placeholder_id(document_id: str) -> bool:
    return document_id == PLACEHOLDER_ID_PREFIX or document_id.startswith(
        f"{PLACEHOLDER_ID_PREFIX}-"
    )


def row_problems(dataset: DocExtractionDataset, *, allow_placeholders: bool = False) -> list[str]:
    """Every gold row on its slot and against its template, in file order."""
    manifest = dataset.manifest
    problems = [f"document id {i} is used more than once" for i in repeated_ids(dataset.rows)]
    for row in dataset.rows:
        where = f"{GOLD_FILE} document {row.id}"
        if not allow_placeholders and _is_placeholder_id(row.id):
            problems.append(f"{where}: is a placeholder id (the README's illustrations)")
        slot = manifest.slot(row.id)
        if slot is None:
            problems.append(f"{where}: no slot has this id (write a document into a slot)")
        else:
            if row.business_id != slot.business_id:
                problems.append(f"{where}: its slot is business {slot.business_id}'s")
            if row.split != slot.split:
                problems.append(f"{where}: its slot's split is {slot.split}")
            missing = [tag for tag in slot.tags if tag not in row.tags]
            if missing:
                problems.append(f"{where}: its slot requires the tag(s) {', '.join(missing)}")
        config = dataset.kind_config(row)
        if config is None:
            problems.append(f"{where}: its business does not enable the kind {row.kind}")
        else:
            problems += gold_problems(row, config, allow_placeholders=allow_placeholders)
    return problems


# ── Documents ─────────────────────────────────────────────────────────────────────────────────


def _squeezed(text: str) -> str:
    return " ".join(text.split())


def _has_residue(text: str, lines: list[str]) -> bool:
    """Whether `text` holds anything once each line is taken out (longest first, so a short
    line inside a longer one cannot break the longer one's match)."""
    for line in sorted(lines, key=len, reverse=True):
        text = text.replace(line, " ", 1)
    return bool(text.strip())


def read_document(pdf: bytes) -> DocumentText | DocumentRefused:
    """The PDF as the core reads it (E12 included), or its refusal."""
    try:
        return read_pages(pdf, limits=DEFAULT_LIMITS)
    except DocumentRefused as refused:
        return refused


def pdf_problems(document_id: str, source: SourceDocument, pdf: bytes) -> list[str]:
    """The PDF against the core's reader and its source (module docstring)."""
    where = f"{DOCUMENTS_DIR}/{document_id}{PDF_SUFFIX}"
    read = read_document(pdf)
    if isinstance(read, DocumentRefused):
        e12 = " (over E12: refused, never cut)" if read.over_limit else ""
        return [f"{where}: the core refuses it{e12}: {read}"]
    rebuild = f"rebuild it from its {SOURCE_SUFFIX} (README, 'Documents')"
    if len(read.pages) != len(source.pages):
        return [
            f"{where}: {len(read.pages)} page(s), its source has {len(source.pages)}; {rebuild}"
        ]
    problems: list[str] = []
    if read.image_pages != source.image_pages:
        problems.append(f"{where}: its image pages differ from its source's; {rebuild}")
    for page, read_page in zip(source.pages, read.pages, strict=True):
        text = _squeezed(read_page.text)
        lines = [_squeezed(line.text) for line in page.lines]
        missing = [i for i, line in enumerate(lines, 1) if line not in text]
        if missing:
            problems.append(
                f"{where}: page {page.number} lacks the text of source line(s) of that page"
                f" {', '.join(map(str, missing))}; {rebuild}"
            )
        elif _has_residue(text, lines):
            problems.append(
                f"{where}: page {page.number} holds text its source does not; {rebuild}"
            )
        on_lines = {finding for line in page.lines for finding in personal_data_findings(line.text)}
        problems += [
            f"{where}: page {page.number} holds what looks like personal data ({finding});"
            " invent it"
            for finding in personal_data_findings(read_page.text)
            if finding not in on_lines  # `source_problems` names those by line
        ]
    return problems


def source_problems(
    row: GoldRow, source: SourceDocument, *, allow_placeholders: bool = False
) -> list[str]:
    """The source against its row's tags, E12's page count, and the content screens."""
    where = f"{DOCUMENTS_DIR}/{row.id}{SOURCE_SUFFIX}"
    problems: list[str] = []
    if len(source.pages) > DEFAULT_LIMITS.max_pages:
        problems.append(
            f"{where}: {len(source.pages)} pages, over E12's {DEFAULT_LIMITS.max_pages} (refused,"
            " never cut)"
        )
    if bool(source.image_pages) != ("image_page" in row.tags):
        problems.append(f"{where}: tagged image_page exactly when a page is marked image")
    if source.has_columns != ("table" in row.tags):
        problems.append(f"{where}: tagged table exactly when a line has a column (x=...)")
    for page in source.pages:
        for number, line in enumerate(page.lines, 1):
            at = f"{where}: page {page.number} line {number}"
            if not allow_placeholders and is_placeholder_text(line.text):
                problems.append(f"{at} is a placeholder (a marker such as {PLACEHOLDER_MARKER})")
            problems += [
                f"{at} holds what looks like personal data ({finding}); invent it"
                for finding in personal_data_findings(line.text)
            ]
    return problems


def folded(text: str) -> str:
    """Whitespace collapsed and case-folded: how a gold value is looked for on a line."""
    return " ".join(text.split()).casefold()


def visible_lines(source: SourceDocument) -> list[str]:
    """Every line a person sees on the page (not `hidden`), `folded`."""
    return [folded(line.text) for page in source.pages for line in page.lines if not line.hidden]


def _whole_token(needle: str, line: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", line) is not None


def presence_problems(row: GoldRow, source: SourceDocument, config: KindConfig) -> list[str]:
    """Each gold value against the lines a person sees (module docstring, "Gold is on the
    page"). Names the document and field, never the value."""
    lines = visible_lines(source)
    problems: list[str] = []
    for spec in config.template.fields:
        value = row.fields.get(spec.name)
        if value is None or spec.type is FieldType.DATE:
            continue
        if spec.type is FieldType.ENUM:
            words = [value] + [
                synonym
                for choice in config.enum_values.get(spec.name, ())
                if choice.id == value
                for synonym in choice.synonyms
            ]
            found = any(_whole_token(folded(word), line) for word in words for line in lines)
        else:
            needle = folded(value)
            found = bool(needle) and any(needle in line for line in lines)
        if not found:
            problems.append(
                f"{GOLD_FILE} document {row.id}: field {spec.name}'s gold is on no visible line"
                f" of its {SOURCE_SUFFIX} (copy it as printed; never from a hidden line or the"
                " image)"
            )
    return problems


def document_problems(
    dataset: DocExtractionDataset, *, allow_placeholders: bool = False
) -> list[str]:
    """Every row's files, then files without a row, then stray files."""
    problems: list[str] = []
    for row in dataset.rows:
        files = dataset.documents.get(row.id, DocumentFiles(None, None))
        if files.source is None:
            problems.append(f"{DOCUMENTS_DIR}/{row.id}{SOURCE_SUFFIX} is missing")
            continue
        try:
            source = parse_source(files.source.decode("utf-8"))
        except UnicodeDecodeError:
            problems.append(f"{DOCUMENTS_DIR}/{row.id}{SOURCE_SUFFIX} is not UTF-8 text")
            continue
        except SourceError as error:
            problems += [f"{DOCUMENTS_DIR}/{row.id}{SOURCE_SUFFIX} {p}" for p in error.problems]
            continue
        problems += source_problems(row, source, allow_placeholders=allow_placeholders)
        config = dataset.kind_config(row)
        if config is not None:  # otherwise `row_problems` names the kind
            problems += presence_problems(row, source, config)
        if files.pdf is None:
            problems.append(
                f"{DOCUMENTS_DIR}/{row.id}{PDF_SUFFIX} is missing: build it from its"
                f" {SOURCE_SUFFIX} (README, 'Documents')"
            )
        else:
            problems += pdf_problems(row.id, source, files.pdf)
    rows = {row.id for row in dataset.rows}
    problems += [
        f"{DOCUMENTS_DIR}/{i}: files without a gold row (add its row, or remove the files)"
        for i in sorted(set(dataset.documents) - rows)
    ]
    problems += [f"{DOCUMENTS_DIR}/{name} is not a document's file" for name in dataset.stray]
    return problems


# ── Minimums, the check and the freeze ────────────────────────────────────────────────────────


def minimums(dataset: DocExtractionDataset) -> list[Minimum]:
    """Every minimum, in a fixed order (zeros included): documents, then per business its
    documents and each tag, then filled slots."""
    need = dataset.manifest.requirements
    rows = dataset.rows
    found = [Minimum("documents", len(rows), need.min_documents)]
    for business_id in dataset.manifest.businesses:
        mine = [r for r in rows if r.business_id == business_id]
        found.append(
            Minimum(f"business {business_id}: documents", len(mine), need.min_per_business)
        )
        found += [
            Minimum(
                f"business {business_id}: tagged {tag}",
                sum(tag in r.tags for r in mine),
                need.min_tag_per_business.get(tag, 0),
            )
            for tag in TAGS
        ]
    filled = {r.id for r in rows} & {s.id for s in dataset.manifest.slots}
    found.append(Minimum("slots filled", len(filled), len(dataset.manifest.slots)))
    return found


def readiness_problems(dataset: DocExtractionDataset) -> list[str]:
    return [f"{m.what}: {m.have} of at least {m.need}" for m in minimums(dataset) if not m.met]


def check_dataset(
    dataset: DocExtractionDataset, *, allow_placeholders: bool = False
) -> CheckResult:
    """Every problem with the version at its stage (the module docstring). `allow_placeholders`
    exists for tests only; the CLI and the dataset test never set it."""
    problems = [
        *slot_problems(dataset.manifest),
        *business_problems(dataset),
        *row_problems(dataset, allow_placeholders=allow_placeholders),
        *document_problems(dataset, allow_placeholders=allow_placeholders),
    ]
    if dataset.manifest.stage == "collecting":
        if dataset.freeze is not None:
            problems.append(f"{FREEZE_FILE} exists, but the manifest's stage is collecting")
    else:
        problems += readiness_problems(dataset)
        if dataset.freeze is None:
            problems.append(f"the manifest's stage is frozen, but there is no {FREEZE_FILE}")
        else:
            problems += freeze_problems(
                dataset.manifest, dataset.freeze, dataset.rows, dataset.pins()
            )
    return CheckResult(problems=tuple(problems), warnings=())


@dataclass(frozen=True)
class FrozenDocExtraction:
    """What `freeze` writes: the manifest (`stage: frozen`) and freeze.json."""

    manifest: DocExtractionManifest
    freeze: DocExtractionFreeze


def freeze_dataset(
    dataset: DocExtractionDataset, *, frozen_on: date, allow_placeholders: bool = False
) -> FrozenDocExtraction:
    """Pin the version. Refused (`FreezeRefused`) unless it is collecting, `check` passes, every
    slot is filled and D8's minimums hold. `allow_placeholders` is for tests only."""
    manifest = dataset.manifest
    if manifest.stage != "collecting":
        raise FreezeRefused([f"{manifest.version} is already frozen"])
    checked = check_dataset(dataset, allow_placeholders=allow_placeholders)
    problems = [*checked.problems, *readiness_problems(dataset)]
    if problems:
        raise FreezeRefused(problems)
    frozen_manifest = DocExtractionManifest.model_validate(
        {**manifest.model_dump(), "stage": "frozen"}
    )
    freeze = build_freeze(frozen_manifest, dataset.rows, dataset.pins(), frozen_on)
    return FrozenDocExtraction(manifest=frozen_manifest, freeze=freeze)

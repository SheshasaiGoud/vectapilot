"""`doc_extraction` v0.1 (M6 PR 4, D8 (b)): the manifest with its slots, and the gold rows.

The files live at the repository root, in `evals/datasets/doc_extraction/<version>/`:
`manifest.json` (`DocExtractionManifest`), `gold.jsonl` (`GoldRow`s), `documents/<id>.pdf.txt`
and `documents/<id>.pdf` (`source.py`) and, once frozen, `freeze.json` (`freeze.py`). The
folder's README documents every field, the scoring rules and the writers' brief.

**Slots, fixed before any document** (D8 (b): split 50/50 before writing). The manifest lists
every document the version will hold: its id, business, the tags it must carry and its split.
A document is written *into* a slot; `check` refuses one that is off its slot (an id no slot has,
another business or split, a required tag missing). Each business-and-tag minimum has a dev and a
test slot, so every tag's misses can show on both splits (`checks.slot_problems`).

**Who writes a document and its gold** (D8 (b), ADR 0012): shesha (and friends), by hand. No agent
writes, completes or suggests a document or a gold value; the schema accepts `shesha` and `friend`
only.

**Gold is canonical** (scoring rules): ISO dates (`2026-11-13`), enum ids (`rabies`), identifiers
and text as printed after their label; `null` when the document does not give the field. What a
value must look like for its field's type is checked against the business's template
(`checks.py`); this module checks shape only and never imports `vectapilot_api.extraction`, so
the eval's own comparison (later in PR 4) stays independent of the code it scores.

Loaders take text and do no I/O (`files.py` reads). Problems name the file, line and fields, never
a value (`EvalModel`'s `hide_input_in_errors`).
"""

import json
from collections import Counter
from datetime import date
from typing import Annotated, Final, Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from vectapilot_api.evalset.corpus import EvalModel, describe_validation_error
from vectapilot_api.evalset.questions import DatasetError, DatasetVersion, Split
from vectapilot_api.gateway.types import BusinessId, Slug

MANIFEST_FILE: Final = "manifest.json"
GOLD_FILE: Final = "gold.jsonl"
FREEZE_FILE: Final = "freeze.json"
DOCUMENTS_DIR: Final = "documents"

Tag = Literal["absent", "confusable", "injection", "image_page", "table"]
"""D8 (b)'s document tags:
- `absent`: a required field is not in the document (its gold is null);
- `confusable`: a value another field could take (two dates, two names) or a swap;
- `injection`: instruction-like text in the document;
- `image_page`: a page with an image (`=== page n image ===`, E4);
- `table`: a label column, then a value column (a line with `x`)."""
TAGS: Final[tuple[Tag, ...]] = ("absent", "confusable", "injection", "image_page", "table")

Author = Literal["shesha", "friend"]

FieldName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$", max_length=40)]
GoldValue = Annotated[str, Field(min_length=1, max_length=200)]
"""A canonical value; at most the longest a template allows (`TEXT_MAX`)."""
Folder = Annotated[str, Field(pattern=r"^[a-z0-9_-]+(/[a-z0-9_-]+)*$")]


def _unique_tags(tags: tuple[Tag, ...]) -> tuple[Tag, ...]:
    if len(set(tags)) != len(tags):
        raise ValueError("each tag is listed once")
    return tags


class Slot(EvalModel):
    """One document's place, fixed before any document is written."""

    id: Slug
    business_id: BusinessId
    tags: tuple[Tag, ...]
    """The tags its document must carry (it may carry more)."""
    split: Split

    _unique = field_validator("tags")(_unique_tags)


class Requirements(EvalModel):
    """What `freeze` needs (D8 (b), decided 2026-11-11). Lowering one is a decision, not an edit
    (the committed version's test pins them)."""

    min_documents: int = Field(ge=1)
    min_per_business: int = Field(ge=1)
    min_tag_per_business: dict[Tag, int]
    """Per business, documents carrying each tag (D8 (b): absent 3, confusable 3, injection 2,
    image_page 2, table 1)."""

    @field_validator("min_tag_per_business")
    @classmethod
    def _counts(cls, minimums: dict[Tag, int]) -> dict[Tag, int]:
        if any(n < 0 for n in minimums.values()):
            raise ValueError("a tag minimum is 0 or more")
        return minimums


class DocExtractionManifest(EvalModel):
    """`manifest.json` of a `doc_extraction` version."""

    dataset: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    version: DatasetVersion
    created: date
    synthetic: Literal[True]
    authored_by: str = Field(min_length=1, max_length=300)
    stage: Literal["collecting", "frozen"]
    corpus: Folder
    """The business manifests' folder, from the repository root (`data/sample`): types."""
    extraction: Folder
    """The extraction configuration, from the repository root (`data/extraction`): templates."""
    businesses: tuple[BusinessId, ...] = Field(min_length=1)
    requirements: Requirements
    slots: tuple[Slot, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(set(self.businesses)) != len(self.businesses):
            raise ValueError("each business is listed once")
        ids = Counter(slot.id for slot in self.slots)
        if any(n > 1 for n in ids.values()):
            raise ValueError("each slot id is listed once")
        if not {slot.business_id for slot in self.slots} <= set(self.businesses):
            raise ValueError("every slot's business is one of the manifest's businesses")
        return self

    def slot(self, slot_id: str) -> Slot | None:
        return next((slot for slot in self.slots if slot.id == slot_id), None)


class GoldRow(EvalModel):
    """One line of `gold.jsonl`: one document's canonical values, written by a person."""

    id: Slug
    """The slot's id; the document is `documents/<id>.pdf.txt` and `documents/<id>.pdf`."""
    business_id: BusinessId
    kind: FieldName
    """One of the business's enabled kinds (`referral_letter`, `vaccination_record`)."""
    fields: dict[FieldName, GoldValue | None] = Field(min_length=1, repr=False)
    """Every field of the kind's template, canonical or null (scoring rules)."""
    tags: tuple[Tag, ...]
    split: Split
    """The slot's split, repeated here so a row read alone says where it belongs."""
    author: Author
    created: date
    synthetic: Literal[True]

    _unique = field_validator("tags")(_unique_tags)

    @field_validator("fields")
    @classmethod
    def _trimmed(cls, fields: dict[str, str | None]) -> dict[str, str | None]:
        for value in fields.values():
            if value is not None and (value != value.strip() or "\n" in value):
                raise ValueError("a gold value has no surrounding whitespace and no line break")
        return fields


def load_manifest(text: str) -> DocExtractionManifest:
    """Parse and validate `manifest.json`. Raises `DatasetError`."""
    try:
        return DocExtractionManifest.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error, "(manifest)")
    raise DatasetError(f"{MANIFEST_FILE} is not a valid doc_extraction manifest: {detail}")


def load_gold(text: str) -> tuple[GoldRow, ...]:
    """Parse `gold.jsonl` (blank lines skipped; an empty file is zero rows). Raises
    `DatasetError` naming every bad line and its failing fields, never the line."""
    rows: list[GoldRow] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(GoldRow.model_validate_json(line))
        except ValidationError as error:
            detail = describe_validation_error(error, "(row)")
            problems.append(f"{GOLD_FILE} line {number} is not a valid gold row: {detail}")
    if problems:
        raise DatasetError("\n".join(problems))
    return tuple(rows)


def dump_gold(row: GoldRow) -> str:
    """One JSONL line, fields in schema order."""
    return json.dumps(row.model_dump(mode="json"), ensure_ascii=False)


def repeated_ids(rows: tuple[GoldRow, ...]) -> list[str]:
    counts = Counter(row.id for row in rows)
    return sorted(i for i, n in counts.items() if n > 1)

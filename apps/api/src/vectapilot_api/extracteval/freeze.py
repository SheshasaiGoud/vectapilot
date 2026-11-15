"""The freeze of a `doc_extraction` version (M6 PR 4): what `freeze.json` pins.

Splits are not assigned here: each document's split is its slot's, fixed in the manifest before
any document was written (D8 (b)). The freeze records, so that every later change shows:
- every document's split and the SHA-256 of its gold row (canonical JSON), its `.pdf.txt` and its
  `.pdf`, byte for byte;
- the slots (canonical JSON): a slot moved to the other split after the documents exist would be
  choosing the test set by hand.
From then on the checks (CI included) fail when anything frozen changes, appears or disappears;
the way out is a new version (README, "Adding a version"). A gold correction after the freeze is
a new version too (scoring rules).

**Not the extraction configuration** (M6 PR 4 review, ananya-rag). Templates and business files
are part of the system under test, not of the dataset: each run's fingerprint holds their SHA-256
(`runinfo.Fingerprint.businesses`), so a change to one (a label or synonym added while tuning on
dev, a new enum value later) is a new config with its own test run, never a new dataset version.
What gold depends on is still checked on every `check`: each gold row against the current
template and business file (`checks.gold_problems`), so a renamed field or a removed enum id
fails. A live run refuses configuration outside the repository and records whether it is the
committed one (`harness.load_live`).
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from pydantic import Field, ValidationError

from vectapilot_api.evalset.corpus import EvalModel, Sha256, describe_validation_error
from vectapilot_api.evalset.questions import DatasetError, DatasetVersion, Split
from vectapilot_api.extracteval.gold import FREEZE_FILE, DocExtractionManifest, GoldRow, Slot
from vectapilot_api.gateway.types import Slug

RULES_NOTE: Final = "make a new version instead (README, 'Adding a version')"


def _canonical_sha256(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def gold_hash(row: GoldRow) -> str:
    """SHA-256 of the whole gold row as canonical JSON (sorted keys, no spaces, UTF-8)."""
    return _canonical_sha256(row.model_dump(mode="json"))


def slots_hash(slots: Sequence[Slot]) -> str:
    return _canonical_sha256([slot.model_dump(mode="json") for slot in slots])


class FrozenDocument(EvalModel):
    split: Split
    gold_sha256: Sha256
    source_sha256: Sha256
    pdf_sha256: Sha256


class DocExtractionFreeze(EvalModel):
    """`freeze.json`: what the frozen version is made of. Written by `freeze`, never by hand."""

    dataset: str = Field(min_length=1)
    version: DatasetVersion
    frozen_on: date
    slots_sha256: Sha256
    documents: dict[Slug, FrozenDocument] = Field(min_length=1)


@dataclass(frozen=True)
class DocumentPins:
    """One document's files as on disk now (None: the file is missing)."""

    source_sha256: str | None
    pdf_sha256: str | None


def load_freeze(text: str) -> DocExtractionFreeze:
    try:
        return DocExtractionFreeze.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error, "(freeze)")
    raise DatasetError(f"{FREEZE_FILE} is not a valid doc_extraction freeze: {detail}")


def build_freeze(
    manifest: DocExtractionManifest,
    rows: Sequence[GoldRow],
    documents: Mapping[str, DocumentPins],
    frozen_on: date,
) -> DocExtractionFreeze:
    """The freeze of a checked version: every row has both files (`checks.freeze_dataset`)."""
    frozen: dict[str, FrozenDocument] = {}
    for row in rows:
        pins = documents[row.id]
        if pins.source_sha256 is None or pins.pdf_sha256 is None:
            raise ValueError(f"document {row.id} is missing a file")
        frozen[row.id] = FrozenDocument(
            split=row.split,
            gold_sha256=gold_hash(row),
            source_sha256=pins.source_sha256,
            pdf_sha256=pins.pdf_sha256,
        )
    return DocExtractionFreeze(
        dataset=manifest.dataset,
        version=manifest.version,
        frozen_on=frozen_on,
        slots_sha256=slots_hash(manifest.slots),
        documents=frozen,
    )


def freeze_problems(
    manifest: DocExtractionManifest,
    freeze: DocExtractionFreeze,
    rows: Sequence[GoldRow],
    documents: Mapping[str, DocumentPins],
) -> list[str]:
    """Everything that differs from the freeze. Each problem says to make a new version."""
    bump = f"{manifest.version} is frozen: {RULES_NOTE}"
    problems: list[str] = []
    if (freeze.dataset, freeze.version) != (manifest.dataset, manifest.version):
        problems.append(f"{FREEZE_FILE} belongs to another dataset or version")
    if freeze.slots_sha256 != slots_hash(manifest.slots):
        problems.append(f"the slots changed after the freeze; {bump}")
    present = {row.id: row for row in rows}
    problems += [
        f"document {i} was frozen and is gone; {bump}"
        for i in sorted(set(freeze.documents) - set(present))
    ]
    for row in rows:
        frozen = freeze.documents.get(row.id)
        pins = documents.get(row.id, DocumentPins(None, None))
        if frozen is None:
            problems.append(f"document {row.id} was added after the freeze; {bump}")
            continue
        if gold_hash(row) != frozen.gold_sha256:
            problems.append(f"document {row.id}'s gold row changed after the freeze; {bump}")
        if pins.source_sha256 != frozen.source_sha256:
            problems.append(f"document {row.id}'s .pdf.txt changed after the freeze; {bump}")
        if pins.pdf_sha256 != frozen.pdf_sha256:
            problems.append(f"document {row.id}'s .pdf changed after the freeze; {bump}")
        if row.split != frozen.split:
            problems.append(f"document {row.id} moved from {frozen.split} to {row.split}; {bump}")
    return problems

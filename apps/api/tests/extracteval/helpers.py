"""Shared helpers for the extraction eval's tests: a throwaway repository and TEST-ONLY documents.

**Every document here is made up by the tests and carries the `TEST-ONLY` marker,** which the
committed dataset's check refuses (`allow_placeholders` is a test-only switch). None is an example
for the writers' brief: they exercise the format, the checks and the freeze, never extraction.

`make_repo` lays out `<tmp>/repo` like the repository: `evals/datasets/doc_extraction/v0.1` (the
committed manifest, optionally with other slots and lower minimums, and an empty gold.jsonl),
`data/extraction` (a copy of the committed configuration) and `data/sample/<business>/
manifest.json` (the committed corpus manifests).
"""

import json
import shutil
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Final

from tests.extracteval.pdfs import pdf_of_text
from tests.tools.helpers import CORPUS_DIR, REPO_ROOT, read_json, write_json
from vectapilot_api.evalset.checks import CheckResult
from vectapilot_api.extracteval.checks import DocExtractionDataset, check_dataset
from vectapilot_api.extracteval.files import read_dataset
from vectapilot_api.extracteval.gold import DOCUMENTS_DIR, GOLD_FILE, MANIFEST_FILE, TAGS

COMMITTED: Final = REPO_ROOT / "evals" / "datasets" / "doc_extraction" / "v0.1"
DATASET_PARTS: Final = ("evals", "datasets", "doc_extraction", "v0.1")
EXTRACTION_DIR: Final = REPO_ROOT / "data" / "extraction"
SUNRISE: Final = "sunrise-clinic"
PAWS: Final = "paws-and-co"
TODAY: Final = date(2026, 11, 13)
LOW_MINIMUMS: Final[Mapping[str, Any]] = {
    "min_documents": 1,
    "min_per_business": 1,
    "min_tag_per_business": dict.fromkeys(TAGS, 0),
}
FOUR_SLOTS: Final[Sequence[Mapping[str, Any]]] = (
    {"id": "s-1", "business_id": SUNRISE, "tags": [], "split": "dev"},
    {"id": "s-2", "business_id": SUNRISE, "tags": [], "split": "test"},
    {"id": "p-1", "business_id": PAWS, "tags": [], "split": "dev"},
    {"id": "p-2", "business_id": PAWS, "tags": [], "split": "test"},
)
"""Four untagged slots, one dev and one test per business: the slots of most tests."""

REFERRAL: Final = """=== page 1 ===
TEST-ONLY referral letter
Dated: 2 March 2026
Referred by: Dr. Oren Vale
Patient name: Mira Fernhall
Date of birth: 14/07/1990
Insurer: Brightcover Health
Policy no: BC/2026/0417
"""
REFERRAL_FIELDS: Final[Mapping[str, str | None]] = {
    "patient_name": "Mira Fernhall",
    "date_of_birth": "1990-07-14",
    "referring_doctor": "Dr. Oren Vale",
    "referral_date": "2026-03-02",
    "insurer": "Brightcover Health",
    "policy_number": "BC/2026/0417",
}

VACCINATION: Final = """=== page 1 ===
TEST-ONLY vaccination record
Pet name: Biscuit
Species: Dog
Owner: Lena Marlow
Vaccine: Rabies
Date given: 05/01/2026
Valid until: 05/01/2027
Issued by: Hillside Vet Clinic
"""
VACCINATION_FIELDS: Final[Mapping[str, str | None]] = {
    "pet_name": "Biscuit",
    "species": "dog",
    "owner_name": "Lena Marlow",
    "vaccine": "rabies",
    "date_given": "2026-01-05",
    "valid_until": "2027-01-05",
    "issuing_clinic": "Hillside Vet Clinic",
}

TABLE_AND_IMAGE: Final = """=== page 1 ===
TEST-ONLY vaccination record
{row=2} Pet name:
{row=3} Species:
{row=4} Owner:
{x=150 row=2} Biscuit
{x=150 row=3} Dog
{x=150 row=4} Lena Marlow
Vaccine: Rabies
Date given: 05/01/2026
{hidden} Valid until: 05/01/2029
Valid until: 05/01/2027
Issued by: Hillside Vet Clinic
=== page 2 image ===
TEST-ONLY attached scan
"""
"""A table, a hidden conflicting line and an image page (tags `table` and `image_page`)."""


def make_repo(
    tmp_path: Path,
    *,
    requirements: Mapping[str, Any] | None = None,
    slots: Sequence[Mapping[str, Any]] | None = None,
) -> Path:
    """`<tmp_path>/repo` laid out like the repository (module docstring); returns the dataset
    folder."""
    root = tmp_path / "repo"
    dataset_dir = root.joinpath(*DATASET_PARTS)
    dataset_dir.mkdir(parents=True)
    manifest = read_json(COMMITTED / MANIFEST_FILE)
    if requirements is not None:
        manifest["requirements"] = dict(requirements)
    if slots is not None:
        manifest["slots"] = [dict(slot) for slot in slots]
    manifest["stage"] = "collecting"
    write_json(dataset_dir / MANIFEST_FILE, manifest)
    (dataset_dir / GOLD_FILE).write_text("", "utf-8")
    shutil.copytree(EXTRACTION_DIR, root / "data" / "extraction")
    for business in (SUNRISE, PAWS):
        folder = root / "data" / "sample" / business
        folder.mkdir(parents=True)
        write_json(folder / "manifest.json", read_json(CORPUS_DIR / business / "manifest.json"))
    return dataset_dir


def small_repo(tmp_path: Path) -> Path:
    """A repository with `FOUR_SLOTS` and `LOW_MINIMUMS`."""
    return make_repo(tmp_path, requirements=LOW_MINIMUMS, slots=FOUR_SLOTS)


def gold(
    doc_id: str,
    business_id: str = SUNRISE,
    *,
    fields: Mapping[str, str | None] | None = None,
    tags: Sequence[str] = (),
    split: str = "dev",
    **changes: Any,
) -> dict[str, Any]:
    """A TEST-ONLY gold row as a JSON object (fields as in the README)."""
    sunrise = business_id == SUNRISE
    default = REFERRAL_FIELDS if sunrise else VACCINATION_FIELDS
    row: dict[str, Any] = {
        "id": doc_id,
        "business_id": business_id,
        "kind": "referral_letter" if sunrise else "vaccination_record",
        "fields": dict(default if fields is None else fields),
        "tags": list(tags),
        "split": split,
        "author": "shesha",
        "created": "2026-11-13",
        "synthetic": True,
    }
    return row | changes


def add_document(
    dataset_dir: Path,
    row: Mapping[str, Any],
    source: str | None,
    *,
    pdf: bytes | None = None,
    build: bool = True,
) -> None:
    """Write a document's files and append its gold row. `pdf` replaces the rebuilt PDF; `build`
    False writes none."""
    documents = dataset_dir / DOCUMENTS_DIR
    documents.mkdir(exist_ok=True)
    doc_id = row["id"]
    if source is not None:
        (documents / f"{doc_id}.pdf.txt").write_text(source, "utf-8")
    if pdf is not None:
        (documents / f"{doc_id}.pdf").write_bytes(pdf)
    elif build and source is not None:
        (documents / f"{doc_id}.pdf").write_bytes(pdf_of_text(source))
    with (dataset_dir / GOLD_FILE).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def filled_repo(tmp_path: Path) -> Path:
    """`small_repo` with all four slots filled by valid TEST-ONLY documents."""
    dataset_dir = small_repo(tmp_path)
    add_document(dataset_dir, gold("s-1"), REFERRAL)
    second = {**REFERRAL_FIELDS, "patient_name": "Nira Fernhall"}
    add_document(
        dataset_dir, gold("s-2", split="test", fields=second), REFERRAL.replace("Mira", "Nira")
    )
    add_document(dataset_dir, gold("p-1", PAWS), VACCINATION)
    add_document(
        dataset_dir, gold("p-2", PAWS, split="test", tags=["table", "image_page"]), TABLE_AND_IMAGE
    )
    return dataset_dir


def read(dataset_dir: Path) -> DocExtractionDataset:
    return read_dataset(dataset_dir)


def check(dataset_dir: Path) -> CheckResult:
    """The check with the TEST-ONLY marker allowed (module docstring)."""
    return check_dataset(read(dataset_dir), allow_placeholders=True)

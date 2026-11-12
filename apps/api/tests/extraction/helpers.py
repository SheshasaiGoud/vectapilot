"""Shared fixtures for the extraction tests: the committed folder and throwaway copies of it."""

import shutil
from pathlib import Path
from typing import Any, Final

from tests.tools.helpers import CORPUS_DIR, REPO_ROOT, read_json, write_json

EXTRACTION_DIR: Final = REPO_ROOT / "data" / "extraction"
REFERRAL: Final = Path("templates") / "human_healthcare" / "referral_letter.json"
VACCINATION: Final = Path("templates") / "veterinary" / "vaccination_record.json"
SUNRISE: Final = Path("businesses") / "sunrise-clinic.json"
PAWS: Final = Path("businesses") / "paws-and-co.json"
BUSINESSES: Final = ("paws-and-co", "sunrise-clinic")
"""The fictional businesses with extraction settings (data, not code: rule 9 holds for src/)."""
MARKER: Final = "zz-value-that-must-not-be-echoed"
"""Written into an invalid file; no error may contain it (errors name locations, never values)."""

__all__ = [
    "BUSINESSES",
    "CORPUS_DIR",
    "EXTRACTION_DIR",
    "MARKER",
    "PAWS",
    "REFERRAL",
    "SUNRISE",
    "VACCINATION",
    "copy_extraction",
    "edit_json",
    "read_json",
    "write_json",
]


def copy_extraction(destination: Path) -> Path:
    """A copy of `data/extraction` at `destination`, returned."""
    shutil.copytree(EXTRACTION_DIR, destination)
    return destination


def edit_json(path: Path, **changes: Any) -> dict[str, Any]:
    """`path`'s object with `changes` applied at the top level, written back and returned."""
    data = read_json(path) | changes
    write_json(path, data)
    return data

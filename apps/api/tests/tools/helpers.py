"""Shared fixtures for the tool tests: the committed sandbox folder and throwaway copies of it."""

import json
import shutil
from pathlib import Path
from typing import Any, Final

from vectapilot_api.tools.config import SEED_FILE, TOOLS_FILE

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
SANDBOX_DIR: Final = REPO_ROOT / "data" / "sandbox"
CORPUS_DIR: Final = REPO_ROOT / "data" / "sample"
BUSINESSES: Final = ("paws-and-co", "sunrise-clinic")
"""The fictional businesses with a sandbox (data, not code: rule 9 holds for src/)."""


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_sandbox(destination: Path, renames: dict[str, str] | None = None) -> Path:
    """Copy `data/sandbox` to `destination`, each business renamed by `renames` (folder and
    `business_id` in both files), and return the copy's folder."""
    shutil.copytree(SANDBOX_DIR, destination)
    for old, new in (renames or {}).items():
        folder = destination / old
        for name in (TOOLS_FILE, SEED_FILE):
            data = read_json(folder / name)
            data["business_id"] = new
            write_json(folder / name, data)
        folder.rename(destination / new)
    return destination

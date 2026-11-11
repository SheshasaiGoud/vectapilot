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


def write_corpus(destination: Path, types: dict[str, str]) -> Path:
    """A corpus folder at `destination` with a minimal, valid manifest per business (`types`:
    business id → business type), as `VECTAPILOT_CORPUS_DIR` needs for a business a test made
    (M5 PR 4: W11 compares its type with its `tools.json`'s). No document is written: the API
    reads only each manifest's type."""
    for business_id, business_type in types.items():
        folder = destination / business_id
        folder.mkdir(parents=True, exist_ok=True)
        write_json(
            folder / "manifest.json",
            {
                "business_id": business_id,
                "name": "A test business",
                "business_type": business_type,
                "synthetic": True,
                "created": "2026-11-10",
                "authored_by": "tests (M5 PR 4)",
                "languages": ["en"],
                "default_language": "en",
                "sources": [
                    {
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
                ],
            },
        )
    return destination


def corpus_matching(destination: Path, sandbox: Path) -> Path:
    """`write_corpus` for every business of the tool folder `sandbox`, each with its own
    `tools.json` business type."""
    types = {
        folder.name: str(read_json(folder / TOOLS_FILE)["business_type"])
        for folder in sorted(sandbox.iterdir())
        if folder.is_dir()
    }
    return write_corpus(destination, types)

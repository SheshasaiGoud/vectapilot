"""Reading and writing a `doc_extraction` version (the I/O shell around the pure modules).

Businesses are read from their configuration (rule 9): the extraction folder
(`<extraction>/templates/...` and `<extraction>/businesses/<business_id>.json`, validated exactly
as the API reads them, `extraction.files.load_extraction_dir`), whose business types are checked
against each corpus manifest (`<corpus>/<business_id>/manifest.json`).

`documents/` may be missing while the version is empty (git keeps no empty folder). Each file is
`<id>.pdf.txt` or `<id>.pdf`; anything else is listed as stray (dot files such as `.DS_Store` are
ignored). Files are read as bytes: `checks.py` decodes the text and reads the PDF.
"""

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from vectapilot_api.evalset.files import repo_root_of
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.extracteval.checks import (
    DocExtractionDataset,
    DocumentFiles,
    FrozenDocExtraction,
)
from vectapilot_api.extracteval.freeze import load_freeze
from vectapilot_api.extracteval.gold import (
    DOCUMENTS_DIR,
    FREEZE_FILE,
    GOLD_FILE,
    MANIFEST_FILE,
    load_gold,
    load_manifest,
)
from vectapilot_api.extracteval.source import PDF_SUFFIX, SOURCE_SUFFIX
from vectapilot_api.extraction.files import load_extraction_dir

DEFAULT_DATASET: Final = Path("evals", "datasets", "doc_extraction", "v0.1")
"""The current version, relative to the repository root."""
_DOCUMENT_ID: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _read(dataset_dir: Path, name: str) -> str:
    path = dataset_dir / name
    if not path.is_file():
        raise DatasetError(f"{name} is missing in {dataset_dir.name} (it may be empty, not absent)")
    return path.read_text("utf-8")


def _document_id(name: str) -> tuple[str, str] | None:
    """`(id, suffix)` for a document's file name, else None."""
    for suffix in (SOURCE_SUFFIX, PDF_SUFFIX):
        if name.endswith(suffix):
            stem = name.removesuffix(suffix)
            if len(stem) <= 64 and _DOCUMENT_ID.fullmatch(stem):
                return stem, suffix
            return None
    return None


def read_documents(folder: Path) -> tuple[dict[str, DocumentFiles], tuple[str, ...]]:
    """Every document's files by id, and the stray names (module docstring)."""
    if not folder.is_dir():
        return {}, ()
    sources: dict[str, bytes] = {}
    pdfs: dict[str, bytes] = {}
    stray: list[str] = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        parsed = _document_id(entry.name) if entry.is_file() else None
        if parsed is None:
            stray.append(entry.name)
            continue
        document_id, suffix = parsed
        (sources if suffix == SOURCE_SUFFIX else pdfs)[document_id] = entry.read_bytes()
    documents = {
        i: DocumentFiles(source=sources.get(i), pdf=pdfs.get(i))
        for i in sorted(set(sources) | set(pdfs))
    }
    return documents, tuple(stray)


def read_dataset(
    dataset_dir: Path, corpus_dir: Path | None = None, extraction_dir: Path | None = None
) -> DocExtractionDataset:
    """The version in `dataset_dir`, its businesses from `extraction_dir` and `corpus_dir`
    (default: the manifest's folders under the repository root). Raises `DatasetError` or
    `ExtractionConfigError`."""
    manifest = load_manifest(_read(dataset_dir, MANIFEST_FILE))
    root = repo_root_of(dataset_dir)
    extraction = load_extraction_dir(
        extraction_dir or root / manifest.extraction, corpus_dir or root / manifest.corpus
    )
    documents, stray = read_documents(dataset_dir / DOCUMENTS_DIR)
    freeze_path = dataset_dir / FREEZE_FILE
    return DocExtractionDataset(
        manifest=manifest,
        rows=load_gold(_read(dataset_dir, GOLD_FILE)),
        documents=documents,
        stray=stray,
        extraction=extraction,
        freeze=load_freeze(freeze_path.read_text("utf-8")) if freeze_path.is_file() else None,
    )


def _json(data: Mapping[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_frozen(dataset_dir: Path, frozen: FrozenDocExtraction) -> None:
    """Write the two files of a freeze: freeze.json and the manifest (`stage: frozen`). Gold rows
    and documents are not rewritten: their splits are their slots'."""
    (dataset_dir / FREEZE_FILE).write_text(_json(frozen.freeze.model_dump(mode="json")), "utf-8")
    (dataset_dir / MANIFEST_FILE).write_text(
        _json(frozen.manifest.model_dump(mode="json")), "utf-8"
    )

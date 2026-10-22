"""Reading and writing the dataset and corpus files (the I/O shell around the pure modules)."""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from vectapilot_api.evalset.checks import Frozen
from vectapilot_api.evalset.corpus import (
    CORPUS_MANIFEST_FILE,
    PDF_TEXT_SUFFIX,
    Corpus,
    CorpusDocument,
    CorpusError,
    CorpusManifest,
    load_business_documents,
    load_corpus_manifest,
)
from vectapilot_api.evalset.questions import (
    FREEZE_FILE,
    MANIFEST_FILE,
    QUESTIONS_FILE,
    EvalManifest,
    EvalQuestion,
    dump_question,
    load_manifest,
    load_questions,
)
from vectapilot_api.evalset.split import FreezeManifest, load_freeze

DEFAULT_DATASET: Final = Path("evals", "datasets", "support_qa", "v0.1")
"""The current version, relative to the repository root."""


def find_repo_root(start: Path) -> Path:
    """The nearest folder at or above `start` that holds `evals/datasets`."""
    for folder in (start.resolve(), *start.resolve().parents):
        if (folder / "evals" / "datasets").is_dir():
            return folder
    raise FileNotFoundError("no evals/datasets folder at or above the current directory")


def repo_root_of(dataset_dir: Path) -> Path:
    """`<root>/evals/datasets/<name>/<version>` → `<root>`."""
    return dataset_dir.resolve().parents[3]


def read_business(folder: Path, business_id: str) -> tuple[CorpusManifest, list[CorpusDocument]]:
    path = folder / CORPUS_MANIFEST_FILE
    if not path.is_file():
        raise CorpusError(f"{business_id} has no {CORPUS_MANIFEST_FILE}")
    manifest = load_corpus_manifest(
        path.read_text("utf-8"), where=f"{business_id}/{CORPUS_MANIFEST_FILE}"
    )
    if manifest.business_id != business_id:
        raise CorpusError(f"{business_id}/{CORPUS_MANIFEST_FILE} names another business")
    for source in manifest.sources:
        if not (folder / source.file).is_file():
            raise CorpusError(f"{business_id}/{source.file} is listed but missing")
    documents = load_business_documents(manifest, lambda name: (folder / name).read_bytes())
    return manifest, list(documents)


def read_corpus(corpus_dir: Path, business_ids: Iterable[str]) -> Corpus:
    """Load the listed businesses (a missing folder is skipped: the check reports it)."""
    manifests: list[CorpusManifest] = []
    documents: list[CorpusDocument] = []
    for business_id in business_ids:
        folder = corpus_dir / business_id
        if folder.is_dir():
            manifest, loaded = read_business(folder, business_id)
            manifests.append(manifest)
            documents += loaded
    return Corpus(manifests, documents)


def unlisted_files(folder: Path, manifest: CorpusManifest) -> list[str]:
    """Files in a business folder that the manifest does not explain: neither the manifest, nor a
    listed document, nor the `.pdf.txt` text source of a listed PDF."""
    listed = {s.file for s in manifest.sources}
    explained = {CORPUS_MANIFEST_FILE, *listed}
    explained |= {f + PDF_TEXT_SUFFIX for f in listed if f.endswith(".pdf")}
    return sorted(p.name for p in folder.iterdir() if p.name not in explained)


@dataclass(frozen=True)
class Dataset:
    directory: Path
    manifest: EvalManifest
    questions: tuple[EvalQuestion, ...]
    freeze: FreezeManifest | None
    previous_freeze: FreezeManifest | None
    corpus: Corpus


def _read_freeze(path: Path) -> FreezeManifest | None:
    return load_freeze(path.read_text("utf-8")) if path.is_file() else None


def read_dataset(dataset_dir: Path, corpus_dir: Path | None = None) -> Dataset:
    manifest = load_manifest((dataset_dir / MANIFEST_FILE).read_text("utf-8"))
    questions = load_questions((dataset_dir / QUESTIONS_FILE).read_text("utf-8"))
    previous = None
    if manifest.previous_version is not None:
        previous = _read_freeze(dataset_dir.parent / manifest.previous_version / FREEZE_FILE)
    corpus_root = corpus_dir or repo_root_of(dataset_dir) / manifest.corpus
    return Dataset(
        directory=dataset_dir,
        manifest=manifest,
        questions=questions,
        freeze=_read_freeze(dataset_dir / FREEZE_FILE),
        previous_freeze=previous,
        corpus=read_corpus(corpus_root, manifest.businesses),
    )


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_questions(dataset_dir: Path, questions: Sequence[EvalQuestion]) -> None:
    text = "".join(dump_question(q) + "\n" for q in questions)
    (dataset_dir / QUESTIONS_FILE).write_text(text, "utf-8")


def write_frozen(dataset_dir: Path, frozen: Frozen) -> None:
    """Write the three files of a freeze: the rows with their splits, freeze.json, the manifest."""
    write_questions(dataset_dir, frozen.questions)
    (dataset_dir / FREEZE_FILE).write_text(_json(frozen.freeze.model_dump(mode="json")), "utf-8")
    (dataset_dir / MANIFEST_FILE).write_text(
        _json(frozen.manifest.model_dump(mode="json")), "utf-8"
    )

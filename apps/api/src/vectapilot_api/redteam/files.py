"""Reading and writing a `redteam` version (the I/O shell around the pure modules).

Businesses come from their corpus manifests (rule 9): the sample businesses from
`<corpus>/<business_id>/manifest.json` (the manifest's `corpus`, `data/sample`), and the
test-only fixture businesses from `<version>/fixtures/<business_id>/manifest.json` (`rt-shop`),
which never live in `data/sample/` or `data/sandbox/`. Of each business the checks need its type,
its languages and the fake contact lines its public, approved documents print (read as text:
Markdown, HTML and CSV as they are, a PDF from its `.pdf.txt` source; `pii.literal_findings`).

Every file under `fixtures/` is read as bytes (dot files such as `.DS_Store` ignored) and checked
by `checks.fixture_file_problems`.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from vectapilot_api.evalset.corpus import (
    CORPUS_MANIFEST_FILE,
    CorpusError,
    CorpusManifest,
    load_corpus_manifest,
)
from vectapilot_api.evalset.files import repo_root_of
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.ingestion.types import SourceType
from vectapilot_api.redteam.cases import (
    CASES_FILE,
    FIXTURES_DIR,
    FREEZE_FILE,
    GATES_FILE,
    MANIFEST_FILE,
    load_gates,
    load_manifest,
    parse_cases,
)
from vectapilot_api.redteam.checks import BusinessInfo, FrozenRedteam, RedteamDataset
from vectapilot_api.redteam.freeze import load_freeze
from vectapilot_api.redteam.pii import literal_findings

DEFAULT_DATASET: Final = Path("evals", "datasets", "redteam", "v0.1")
"""The current version, relative to the repository root."""
PDF_TEXT_SUFFIX: Final = ".txt"


def _read(dataset_dir: Path, name: str) -> bytes:
    path = dataset_dir / name
    if not path.is_file():
        raise DatasetError(f"{name} is missing in {dataset_dir.name} (it may be empty, not absent)")
    return path.read_bytes()


def _text(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise DatasetError(f"{name} is not UTF-8 text") from None


def _published(folder: Path, manifest: CorpusManifest) -> frozenset[str]:
    """Keys of the fake contact literals the business's public, approved documents print."""
    keys: set[str] = set()
    for source in manifest.sources:
        if not source.answers_customers:
            continue
        name = source.file + (PDF_TEXT_SUFFIX if source.format is SourceType.PDF else "")
        path = folder / name
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        keys.update(f.key for f in literal_findings(text) if f.fake)
    return frozenset(keys)


def read_business(folder: Path, business_id: str, *, fixture: bool) -> BusinessInfo:
    """One business's `BusinessInfo`. Raises `CorpusError` naming the business, never content."""
    path = folder / CORPUS_MANIFEST_FILE
    if not path.is_file():
        raise CorpusError(f"{business_id} has no {CORPUS_MANIFEST_FILE}")
    manifest = load_corpus_manifest(path.read_text("utf-8"), where=f"{business_id}/manifest")
    if manifest.business_id != business_id:
        raise CorpusError(f"{business_id}/{CORPUS_MANIFEST_FILE} names another business")
    return BusinessInfo(
        business_id=business_id,
        business_type=manifest.business_type,
        languages=manifest.languages,
        published=_published(folder, manifest),
        fixture=fixture,
    )


def read_businesses(
    corpus_dir: Path, fixtures_dir: Path, fixture_ids: tuple[str, ...]
) -> tuple[dict[str, BusinessInfo], list[str]]:
    """Every sample business (each folder of `corpus_dir` with a manifest) and every listed
    fixture business, and the problems reading them."""
    businesses: dict[str, BusinessInfo] = {}
    problems: list[str] = []
    if corpus_dir.is_dir():
        for folder in sorted(p for p in corpus_dir.iterdir() if p.is_dir()):
            if (folder / CORPUS_MANIFEST_FILE).is_file():
                try:
                    businesses[folder.name] = read_business(folder, folder.name, fixture=False)
                except CorpusError as error:
                    problems.append(str(error))
    for business_id in fixture_ids:
        if business_id in businesses:
            problems.append(f"fixture business {business_id} shadows a sample business")
            continue
        try:
            businesses[business_id] = read_business(
                fixtures_dir / business_id, business_id, fixture=True
            )
        except CorpusError as error:
            problems.append(f"{FIXTURES_DIR}/{error}")
    return businesses, problems


def read_fixture_files(fixtures_dir: Path) -> dict[str, bytes]:
    """Every file under `fixtures/`, by its path from the version folder."""
    if not fixtures_dir.is_dir():
        return {}
    return {
        f"{FIXTURES_DIR}/{path.relative_to(fixtures_dir).as_posix()}": path.read_bytes()
        for path in sorted(fixtures_dir.rglob("*"))
        if path.is_file() and not any(part.startswith(".") for part in path.parts)
    }


def read_dataset(dataset_dir: Path, corpus_dir: Path | None = None) -> RedteamDataset:
    """The version in `dataset_dir`, its sample businesses from `corpus_dir` (default: the
    manifest's folder under the repository root). Raises `DatasetError`."""
    manifest = load_manifest(_text(_read(dataset_dir, MANIFEST_FILE), MANIFEST_FILE))
    cases_bytes = _read(dataset_dir, CASES_FILE)
    gates_bytes = _read(dataset_dir, GATES_FILE)
    cases, parse_problems = parse_cases(_text(cases_bytes, CASES_FILE))
    fixtures_dir = dataset_dir / FIXTURES_DIR
    corpus = corpus_dir or repo_root_of(dataset_dir) / manifest.corpus
    businesses, business_problems = read_businesses(
        corpus, fixtures_dir, manifest.fixture_businesses
    )
    freeze_path = dataset_dir / FREEZE_FILE
    return RedteamDataset(
        manifest=manifest,
        cases=cases,
        parse_problems=parse_problems,
        gates=load_gates(_text(gates_bytes, GATES_FILE)),
        cases_bytes=cases_bytes,
        gates_bytes=gates_bytes,
        businesses=businesses,
        business_problems=tuple(business_problems),
        fixture_files=read_fixture_files(fixtures_dir),
        freeze=load_freeze(freeze_path.read_text("utf-8")) if freeze_path.is_file() else None,
    )


def _json(data: Mapping[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_frozen(dataset_dir: Path, frozen: FrozenRedteam) -> None:
    """Write the two files of a freeze: freeze.json and the manifest (`stage: frozen`, counts).
    Cases and gates are not rewritten: the freeze pins them as they are."""
    (dataset_dir / FREEZE_FILE).write_text(_json(frozen.freeze.model_dump(mode="json")), "utf-8")
    (dataset_dir / MANIFEST_FILE).write_text(
        _json(frozen.manifest.model_dump(mode="json", exclude_none=False)), "utf-8"
    )

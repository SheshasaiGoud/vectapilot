"""Reading and writing an `intents` version (the I/O shell around the pure modules).

Businesses are read from their configuration, `<corpus>/<business_id>/manifest.json` (rule 9):
only `business_type` and `languages` are used, and no document is loaded.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final

from vectapilot_api.evalset.corpus import CORPUS_MANIFEST_FILE, CorpusError, load_corpus_manifest
from vectapilot_api.evalset.files import repo_root_of
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.intenteval.checks import FrozenIntents, IntentDataset
from vectapilot_api.intenteval.rows import (
    EVAL_FILE,
    FREEZE_FILE,
    MANIFEST_FILE,
    SAMPLE_FILE,
    TRAIN_FILE,
    BusinessProfile,
    dump_row,
    load_eval,
    load_manifest,
    load_train,
)
from vectapilot_api.intenteval.sample import load_sample, render_sample
from vectapilot_api.intenteval.split import load_freeze

DEFAULT_DATASET: Final = Path("evals", "datasets", "intents", "v0.1")
"""The current version, relative to the repository root."""


def read_businesses(corpus_dir: Path, business_ids: Iterable[str]) -> dict[str, BusinessProfile]:
    """Each listed business's type and languages (a missing folder is skipped: the check reports
    it). Raises `CorpusError` for an invalid manifest or one that names another business."""
    profiles: dict[str, BusinessProfile] = {}
    for business_id in business_ids:
        path = corpus_dir / business_id / CORPUS_MANIFEST_FILE
        if not path.is_file():
            continue
        where = f"{business_id}/{CORPUS_MANIFEST_FILE}"
        manifest = load_corpus_manifest(path.read_text("utf-8"), where=where)
        if manifest.business_id != business_id:
            raise CorpusError(f"{where} names another business")
        profiles[business_id] = BusinessProfile(
            business_type=manifest.business_type, languages=manifest.languages
        )
    return profiles


def _read(dataset_dir: Path, name: str) -> bytes:
    path = dataset_dir / name
    if not path.is_file():
        raise DatasetError(f"{name} is missing in {dataset_dir.name} (it may be empty, not absent)")
    return path.read_bytes()


def read_dataset(dataset_dir: Path, corpus_dir: Path | None = None) -> IntentDataset:
    manifest = load_manifest(_read(dataset_dir, MANIFEST_FILE).decode("utf-8"))
    train_bytes = _read(dataset_dir, TRAIN_FILE)
    freeze_path = dataset_dir / FREEZE_FILE
    sample_path = dataset_dir / SAMPLE_FILE
    corpus_root = corpus_dir or repo_root_of(dataset_dir) / manifest.corpus
    return IntentDataset(
        manifest=manifest,
        train=load_train(train_bytes.decode("utf-8")),
        eval_rows=load_eval(_read(dataset_dir, EVAL_FILE).decode("utf-8")),
        businesses=read_businesses(corpus_root, manifest.businesses),
        freeze=load_freeze(freeze_path.read_text("utf-8")) if freeze_path.is_file() else None,
        sample=load_sample(sample_path.read_text("utf-8")) if sample_path.is_file() else None,
        train_sha256=hashlib.sha256(train_bytes).hexdigest(),
    )


def _json(data: Mapping[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_sample(dataset_dir: Path, ids: Sequence[str]) -> None:
    (dataset_dir / SAMPLE_FILE).write_text(render_sample(ids), "utf-8")


def write_frozen(dataset_dir: Path, frozen: FrozenIntents) -> None:
    """Write the three files of a freeze: the eval rows with their splits, freeze.json and the
    manifest. `train.jsonl` is not touched."""
    text = "".join(dump_row(r) + "\n" for r in frozen.eval_rows)
    (dataset_dir / EVAL_FILE).write_text(text, "utf-8")
    (dataset_dir / FREEZE_FILE).write_text(_json(frozen.freeze.model_dump(mode="json")), "utf-8")
    (dataset_dir / MANIFEST_FILE).write_text(
        _json(frozen.manifest.model_dump(mode="json")), "utf-8"
    )

"""M4 PR 1: reading a version and its businesses, writing a sample and a freeze."""

import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from tests.intenteval.helpers import (
    BUSINESSES,
    CORPUS_DIR,
    DATASET_DIR,
    PROFILES,
    complete_dataset,
    train_data,
)
from vectapilot_api.evalset.corpus import CorpusError
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.intenteval.checks import freeze_dataset
from vectapilot_api.intenteval.files import (
    read_businesses,
    read_dataset,
    write_frozen,
    write_sample,
)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    copy = tmp_path / "v0.1"
    copy.mkdir()
    for name in ("manifest.json", "train.jsonl", "eval.jsonl"):
        shutil.copy(DATASET_DIR / name, copy)
    return copy


def test_businesses_come_from_their_manifests_in_data_sample() -> None:
    assert read_businesses(CORPUS_DIR, BUSINESSES) == PROFILES


def test_a_missing_business_folder_is_skipped_for_the_check_to_report() -> None:
    assert read_businesses(CORPUS_DIR, ("no-such-business",)) == {}


def test_a_manifest_that_names_another_business_is_refused(tmp_path: Path) -> None:
    shutil.copytree(CORPUS_DIR / "paws-and-co", tmp_path / "other-vet")
    with pytest.raises(CorpusError, match=r"other-vet/manifest\.json names another business"):
        read_businesses(tmp_path, ("other-vet",))


def test_a_version_reads_with_its_hash_freeze_and_sample(folder: Path) -> None:
    line = json.dumps(train_data()) + "\n"
    (folder / "train.jsonl").write_text(line, "utf-8")
    write_sample(folder, ["example-t1"])
    loaded = read_dataset(folder, CORPUS_DIR)
    assert [r.id for r in loaded.train] == ["example-t1"]
    assert loaded.sample == ("example-t1",)
    assert loaded.freeze is None
    assert loaded.train_sha256 == hashlib.sha256(line.encode()).hexdigest()
    assert loaded.businesses == PROFILES


def test_a_missing_data_file_is_a_dataset_error(folder: Path) -> None:
    (folder / "eval.jsonl").unlink()
    with pytest.raises(DatasetError, match=r"eval\.jsonl is missing"):
        read_dataset(folder, CORPUS_DIR)


def test_a_freeze_writes_three_files_and_leaves_train_alone(folder: Path) -> None:
    frozen = freeze_dataset(
        complete_dataset(), frozen_on=date(2026, 11, 20), allow_placeholders=True
    )
    train_before = (folder / "train.jsonl").read_bytes()
    write_frozen(folder, frozen)
    assert (folder / "train.jsonl").read_bytes() == train_before
    assert json.loads((folder / "manifest.json").read_text("utf-8"))["stage"] == "frozen"
    assert json.loads((folder / "freeze.json").read_text("utf-8"))["frozen_on"] == "2026-11-20"
    lines = (folder / "eval.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == len(frozen.eval_rows)
    assert all(json.loads(line)["split"] in ("dev", "test") for line in lines)

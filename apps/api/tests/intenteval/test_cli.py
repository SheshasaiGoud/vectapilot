"""M4 PR 1: `python -m vectapilot_api.intenteval` on a copy of the dataset folder (never the real
one, so these tests do not change as rows are added)."""

import functools
import io
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from tests.intenteval.helpers import (
    CORPUS_DIR,
    DATASET_DIR,
    REPO_ROOT,
    SMALL_REQUIREMENTS,
    complete_eval,
    complete_train,
    eval_data,
    manifest_data,
    train_data,
)
from vectapilot_api.evalset.checks import FreezeRefused
from vectapilot_api.intenteval import checks, cli
from vectapilot_api.intenteval.checks import FrozenIntents, IntentDataset, check_dataset
from vectapilot_api.intenteval.cli import main
from vectapilot_api.intenteval.files import read_dataset
from vectapilot_api.intenteval.rows import dump_row
from vectapilot_api.intenteval.sample import draw_sample, render_sample


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """The real manifest (its rules) with empty row files: the committed rows are never copied,
    so these tests do not change as rows are added (test_dataset_intents_v0_1 checks those)."""
    copy = tmp_path / "v0.1"
    copy.mkdir()
    shutil.copy(DATASET_DIR / "manifest.json", copy)
    for name in ("train.jsonl", "eval.jsonl"):
        (copy / name).write_text("", "utf-8")
    return copy


def run(folder: Path, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(["--dataset", str(folder), "--corpus", str(CORPUS_DIR), *args], out)
    return code, out.getvalue()


def files_in(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in folder.iterdir()}


def write_rows(folder: Path, name: str, rows: list[dict[str, object]]) -> None:
    (folder / name).write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")


def test_check_passes_an_empty_collecting_version(folder: Path) -> None:
    assert run(folder, "check") == (0, "check (collecting, 0 train, 0 eval): ok\n")


def test_check_lists_problems_exits_1_and_never_edits_a_row(folder: Path) -> None:
    write_rows(folder, "train.jsonl", [train_data(id="example-t1")])
    write_rows(folder, "eval.jsonl", [eval_data(id="example-e1", split="dev")])
    before = files_in(folder)
    code, out = run(folder, "check")
    assert code == 1
    assert "PROBLEM  train.jsonl row example-t1: is a placeholder" in out
    assert "PROBLEM  eval.jsonl row example-e1: split is set by `freeze`" in out
    assert out.endswith("check (collecting, 1 train, 1 eval): 3 problem(s)\n")
    assert files_in(folder) == before


def test_a_file_that_does_not_parse_exits_2(folder: Path) -> None:
    (folder / "eval.jsonl").write_text("{}\n", "utf-8")
    code, out = run(folder, "check")
    assert code == 2
    assert out.startswith("error: eval.jsonl line 1 is not a valid row: ")


def test_report_prints_the_tables(folder: Path) -> None:
    code, out = run(folder, "report")
    assert code == 0
    assert out.startswith("intents v0.1 (collecting, synthetic): 0 train rows, 0 eval rows\n")


def test_sample_refuses_without_training_rows_and_writes_nothing(folder: Path) -> None:
    before = files_in(folder)
    assert run(folder, "sample") == (
        1,
        "no training rows yet: nothing to sample, nothing written\n",
    )
    assert files_in(folder) == before


def test_sample_writes_the_seeded_list(folder: Path) -> None:
    train = complete_train()
    (folder / "train.jsonl").write_text("".join(dump_row(r) + "\n" for r in train), "utf-8")
    code, out = run(folder, "sample")
    assert code == 0
    assert out.startswith("wrote verification-sample.txt: 16 of 16 training rows")
    expected = draw_sample(train, read_dataset(folder, CORPUS_DIR).manifest.verification)
    assert (folder / "verification-sample.txt").read_text("utf-8") == render_sample(expected)


def test_sample_refuses_a_frozen_version(folder: Path) -> None:
    (folder / "manifest.json").write_text(json.dumps(manifest_data(stage="frozen")), "utf-8")
    assert run(folder, "sample") == (1, "v0.1 is frozen: its sample is fixed\n")


def test_freeze_refuses_an_empty_version_and_writes_nothing(folder: Path) -> None:
    before = files_in(folder)
    code, out = run(folder, "freeze", "--today", "2026-11-20")
    assert code == 1
    assert "PROBLEM  eval rows: 0 of at least 160\n" in out
    assert "way out" not in out  # only a balance refusal is solved by one row more or less
    assert out.endswith("freeze refused: nothing was written\n")
    assert files_in(folder) == before


def test_freeze_writes_the_three_files_when_everything_holds(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CLI never allows placeholders, and these rows are placeholders, so the freeze is run
    # with the tests' switch; everything else is the real command.
    monkeypatch.setattr(
        cli, "freeze_dataset", functools.partial(checks.freeze_dataset, allow_placeholders=True)
    )
    train = complete_train()
    (folder / "manifest.json").write_text(
        json.dumps(manifest_data(requirements=SMALL_REQUIREMENTS)), "utf-8"
    )
    (folder / "train.jsonl").write_text("".join(dump_row(r) + "\n" for r in train), "utf-8")
    evals = "".join(dump_row(r) + "\n" for r in complete_eval())
    (folder / "eval.jsonl").write_text(evals, "utf-8")
    assert run(folder, "sample")[0] == 0
    code, out = run(folder, "freeze", "--today", "2026-11-20")
    assert code == 0, out
    assert out.startswith("frozen intents v0.1: 9 dev, 9 test.")
    assert out.endswith("commit eval.jsonl, freeze.json and manifest.json together.\n")
    frozen = read_dataset(folder, CORPUS_DIR)
    assert frozen.manifest.stage == "frozen"
    assert frozen.freeze is not None
    assert check_dataset(frozen, allow_placeholders=True).ok
    assert run(folder, "freeze", "--today", "2026-11-21")[0] == 1  # once per version


def test_an_unbalanced_freeze_prints_the_way_out_and_writes_nothing(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unbalanced(dataset: IntentDataset, *, frozen_on: date) -> FrozenIntents:
        raise FreezeRefused(["intent complaint: 0 of 2 in test, fewer than 1"], unbalanced=True)

    monkeypatch.setattr(cli, "freeze_dataset", unbalanced)
    before = files_in(folder)
    assert run(folder, "freeze", "--today", "2026-11-20") == (
        1,
        "PROBLEM  intent complaint: 0 of 2 in test, fewer than 1\n"
        "PROBLEM  way out: add or remove one eval row and run freeze again; never change the "
        "seed or the percentage (README, 'Split rule and freeze')\n"
        "freeze refused: nothing was written\n",
    )
    assert files_in(folder) == before


def test_the_default_dataset_is_found_from_inside_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO_ROOT / "apps" / "api")
    out = io.StringIO()
    assert main(["check"], out) == 0, out.getvalue()

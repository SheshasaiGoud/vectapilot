"""M6 PR 4: `python -m vectapilot_api.extracteval check | report | freeze` (`extracteval/cli.py`).

`check` prints problems, never a value; exit 0 ok, 1 problems or refused, 2 unreadable. The CLI's
freeze never allows the TEST-ONLY marker, so its success case writes marker-free TEST documents
into `tmp_path` only.
"""

import io
import json
import re
from pathlib import Path

from tests.extracteval.helpers import (
    COMMITTED,
    PAWS,
    REFERRAL,
    VACCINATION,
    add_document,
    gold,
    make_repo,
    small_repo,
)
from vectapilot_api.extracteval.cli import main
from vectapilot_api.extracteval.gold import FREEZE_FILE, GOLD_FILE, MANIFEST_FILE


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out=out)
    return code, out.getvalue()


def test_check_and_report_on_the_committed_version() -> None:
    # The shape, not the count: shesha's documents land one commit at a time (M6 PR 4 review).
    code, text = run("--dataset", str(COMMITTED), "check")
    assert code == 0
    assert re.fullmatch(r"check \((collecting|frozen), \d+ documents in 24 slots\): ok\n", text)
    code, text = run("--dataset", str(COMMITTED), "report", "--counts")
    assert code == 0 and re.search(r"\b\d+ of 24 slots filled", text)


def test_check_prints_problems_and_never_a_value(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    add_document(dataset_dir, gold("s-1", fields={"patient_name": "Zed Quarrow"}), REFERRAL)

    code, text = run("--dataset", str(dataset_dir), "check")

    assert code == 1
    assert "PROBLEM  gold.jsonl document s-1: field date_of_birth is missing" in text
    assert "PROBLEM  documents/s-1.pdf.txt: page 1 line 1 is a placeholder" in text
    assert text.endswith("problem(s)\n")
    assert "Zed" not in text and "Mira" not in text


def test_unreadable_files_exit_2(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    (dataset_dir / GOLD_FILE).write_text("{not json\n", "utf-8")
    code, text = run("--dataset", str(dataset_dir), "check")
    assert code == 2 and text.startswith("error: gold.jsonl line 1 is not a valid gold row")

    (dataset_dir / GOLD_FILE).unlink()
    assert run("--dataset", str(dataset_dir), "check")[0] == 2

    other = make_repo(tmp_path / "other")
    business = other.parents[3] / "data" / "extraction" / "businesses" / "paws-and-co.json"
    business.write_text(
        business.read_text("utf-8").replace('"synthetic": true', '"synthetic": false')
    )
    code, text = run("--dataset", str(other), "check")
    assert code == 2 and "synthetic: true" in text


def test_report_refuses_an_unknown_id(tmp_path: Path) -> None:
    code, text = run("--dataset", str(small_repo(tmp_path)), "report", "--id", "s-7")
    assert (code, text) == (2, "error: no gold row with id s-7\n")


def test_freeze_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    dataset_dir = make_repo(tmp_path)
    before = (dataset_dir / MANIFEST_FILE).read_bytes()

    code, text = run("--dataset", str(dataset_dir), "freeze")

    assert code == 1
    assert "PROBLEM  slots filled: 0 of at least 24\n" in text
    assert text.endswith("freeze refused: nothing was written\n")
    assert (dataset_dir / MANIFEST_FILE).read_bytes() == before
    assert not (dataset_dir / FREEZE_FILE).exists()


def test_freeze_writes_the_freeze_and_the_manifest(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    # Marker-free TEST documents, in tmp_path only (module docstring).
    referral = REFERRAL.replace("TEST-ONLY ", "")
    vaccination = VACCINATION.replace("TEST-ONLY ", "")
    for doc_id, business_id, split, source in (
        ("s-1", "sunrise-clinic", "dev", referral),
        ("s-2", "sunrise-clinic", "test", referral.replace("Policy no", "Policy number")),
        ("p-1", PAWS, "dev", vaccination),
        ("p-2", PAWS, "test", vaccination.replace("Species", "Animal")),
    ):
        add_document(dataset_dir, gold(doc_id, business_id, split=split), source)

    code, text = run("--dataset", str(dataset_dir), "freeze", "--today", "2026-11-14")

    assert code == 0, text
    assert text == (
        "frozen doc_extraction v0.1: 2 dev, 2 test. Review the diff and commit freeze.json and"
        " manifest.json together.\n"
    )
    assert json.loads((dataset_dir / MANIFEST_FILE).read_text("utf-8"))["stage"] == "frozen"
    assert json.loads((dataset_dir / FREEZE_FILE).read_text("utf-8"))["frozen_on"] == "2026-11-14"
    assert run("--dataset", str(dataset_dir), "check")[0] == 0

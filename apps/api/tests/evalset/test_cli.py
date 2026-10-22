"""M2 PR 7: `python -m vectapilot_api.evalset` on a copy of the dataset folder (never the real
one, so these tests do not change as questions are added)."""

import io
import json
import shutil
from datetime import date
from pathlib import Path
from typing import Final

import pytest

from tests.evalset.helpers import (
    CORPUS_DIR,
    DATASET_DIR,
    REPO_ROOT,
    SMALL_REQUIREMENTS,
    copy_reviewed_corpus,
    manifest_data,
    one_per_category,
)
from vectapilot_api.evalset import cli
from vectapilot_api.evalset.checks import FreezeRefused, SplitPreview
from vectapilot_api.evalset.cli import WAY_OUT, FriendDrafts, import_friends, main
from vectapilot_api.evalset.files import find_repo_root, write_questions
from vectapilot_api.evalset.questions import DatasetError

TODAY: Final = date(2026, 10, 23)
HEADER: Final = "business_id,language,question,comment\n"
BUSINESSES: Final = ("sunrise-clinic", "paws-and-co")


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    folder = tmp_path / "v0.1"
    folder.mkdir()
    shutil.copy(DATASET_DIR / "manifest.json", folder)
    (folder / "questions.jsonl").write_text("")
    return folder


def run(dataset: Path, *args: str, corpus: Path = CORPUS_DIR) -> tuple[int, str]:
    out = io.StringIO()
    code = main(["--dataset", str(dataset), "--corpus", str(corpus), *args], out)
    return code, out.getvalue()


def files_in(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in folder.iterdir()}


def test_check_passes_an_empty_collecting_version(dataset: Path) -> None:
    assert run(dataset, "check") == (0, "check (collecting, 0 questions): ok\n")


def test_check_lists_problems_and_exits_1(dataset: Path) -> None:
    row = {
        "id": "sunrise-001",
        "business_id": "sunrise-clinic",
        "question": "TEST-ONLY 1",
        "language": "en",
        "category": "faq",
        "author_type": "team",
        "saw_documents": True,
        "expected_outcome": "answered",
        "created": "2026-10-22",
        "synthetic": True,
        "expected_evidence": [
            [{"source_id": "faq", "version": "2026-10", "anchor": "Saturday: 9:00 to 14:00"}]
        ],
    }
    (dataset / "questions.jsonl").write_text(json.dumps(row) + "\n")
    code, out = run(dataset, "check", "--max-chars", "200", "--overlap", "0")
    assert code == 1
    assert "PROBLEM  question sunrise-001: is a placeholder" in out
    assert out.endswith("check (collecting, 1 questions): 1 problem(s)\n")


def test_ready_lists_what_is_missing(dataset: Path) -> None:
    code, out = run(dataset, "ready")
    assert code == 1
    assert "MISSING  questions: 0 of at least 40\n" in out
    assert (
        "REVIEW   sunrise-clinic/faq-hi.md (faq-hi 2026-10) is review: pending; shesha reviews "
        "it and sets `review` to done in its manifest entry before the freeze\n"
    ) in out
    assert out.endswith("ready: no\n")


def test_ready_prints_the_way_out_when_the_split_is_unbalanced(
    dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview = SplitPreview(
        splits={}, rows=(("total", 0, 0),), problems=("category faq: 0 of 3 in test",)
    )
    monkeypatch.setattr(cli, "preview_splits", lambda *_: preview)
    _, out = run(dataset, "ready")
    assert f"BALANCE  category faq: 0 of 3 in test\nBALANCE  {WAY_OUT}\n" in out
    assert "never change the seed" in WAY_OUT


def test_ready_previews_the_split_per_group_and_writes_nothing(dataset: Path) -> None:
    rows = [
        {
            "id": f"example-{i}",
            "business_id": ("sunrise-clinic", "paws-and-co")[i % 2],
            "question": f"TEST-ONLY {i}",
            "language": "en",
            "category": "out_of_scope",
            "author_type": "team",
            "saw_documents": True,
            "expected_outcome": "dont_know",
            "created": "2026-10-22",
            "synthetic": True,
        }
        for i in range(10)
    ]
    (dataset / "questions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    before = {p.name: p.read_bytes() for p in dataset.iterdir()}
    code, out = run(dataset, "ready")
    assert code == 1  # placeholders, and far from the requirements
    assert (
        "split preview: a dry run of stratified-systematic-v2 (30 % test); nothing written" in out
    )
    assert "\n  total                                 7    3\n" in out  # round(10 * 0.3) = 3
    assert "\n  category out_of_scope                 7    3\n" in out
    assert "BALANCE" not in out
    assert {p.name: p.read_bytes() for p in dataset.iterdir()} == before


def test_report_prints_the_counts(dataset: Path) -> None:
    code, out = run(dataset, "report")
    assert code == 0
    assert out.startswith("support_qa v0.1 (collecting, synthetic): 0 questions\n")


def test_lookup_shows_status_and_marks_what_never_answers(dataset: Path) -> None:
    code, out = run(dataset, "lookup", "sunrise-clinic", "staff and their")
    assert code == 0
    assert "staff-handbook 2026-10 · approved · staff_only" in out
    assert "<< NEVER ANSWERS A CUSTOMER" in out
    assert "1 line(s)." in out
    code, out = run(dataset, "lookup", "paws-and-co", "Consultation (pet bird)")
    assert "fees 2026-07 · approved · public · fees.csv · row 3 · chunks [2]" in out
    assert run(dataset, "lookup", "northwind-shop", "fees") == (
        1,
        "no corpus for business northwind-shop\n",
    )


def test_show_prints_a_document_chunk_by_chunk(dataset: Path) -> None:
    code, out = run(dataset, "show", "sunrise-clinic", "fees", "--version", "2026-04")
    assert code == 0
    assert out.splitlines()[0] == (
        "sunrise-clinic · fees 2026-04 · approved · public · pricing · en · fees-2026-04.pdf"
    )
    assert "── chunk 0 · chars 0-" in out
    assert "page 2 ──" in out
    code, out = run(dataset, "show", "sunrise-clinic", "fees")
    assert (code, out) == (1, "2 documents match; versions of fees: ['2026-04', '2025-10']\n")
    code, out = run(dataset, "show", "paws-and-co", "faq")
    assert "Paws & Co: frequently asked questions > Opening hours" in out


def test_freeze_refuses_an_empty_version_and_writes_nothing(dataset: Path) -> None:
    before = files_in(dataset)
    code, out = run(dataset, "freeze", "--today", "2026-11-01")
    assert code == 1
    assert out.endswith("freeze refused: nothing was written\n")
    assert files_in(dataset) == before


def test_freeze_refuses_placeholder_rows_and_writes_nothing(tmp_path: Path) -> None:
    # Mutation audit M5b: the CLI never lets placeholders through. Nothing else stops this
    # freeze (small requirements, a reviewed corpus; test_checks.py freezes the same rows with
    # allow_placeholders), so the placeholders are the only reason it is refused.
    corpus = copy_reviewed_corpus(tmp_path / "sample")
    folder = tmp_path / "v0.1"
    folder.mkdir()
    (folder / "manifest.json").write_text(
        json.dumps(manifest_data(requirements=SMALL_REQUIREMENTS))
    )
    write_questions(folder, one_per_category())
    before = files_in(folder)
    code, out = run(folder, "freeze", "--today", "2026-11-01", corpus=corpus)
    assert code == 1
    problems = [line for line in out.splitlines() if line.startswith("PROBLEM")]
    assert problems == [
        f"PROBLEM  question {q.id}: is a placeholder (a marker such as EXAMPLE-ONLY)"
        for q in one_per_category()
    ]
    assert out.endswith("freeze refused: nothing was written\n")
    assert files_in(folder) == before


def test_freeze_prints_the_way_out_when_only_the_balance_fails(
    dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise FreezeRefused(["business example-shop: 0 of 4 in test"], unbalanced=True)

    monkeypatch.setattr(cli, "freeze_dataset", refuse)
    code, out = run(dataset, "freeze", "--today", "2026-11-01")
    assert code == 1
    assert out == (
        "PROBLEM  business example-shop: 0 of 4 in test\n"
        f"PROBLEM  {WAY_OUT}\n"
        "freeze refused: nothing was written\n"
    )


def test_a_broken_file_is_an_error_with_exit_2(dataset: Path) -> None:
    (dataset / "manifest.json").write_text("{}")
    code, out = run(dataset, "check")
    assert code == 2
    assert out.startswith("error: manifest.json is not a valid manifest: ")


def test_without_dataset_it_finds_the_repository_and_checks_the_real_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO_ROOT / "apps" / "api")
    out = io.StringIO()
    assert main(["check"], out) == 0, out.getvalue()
    assert find_repo_root(Path.cwd()) == REPO_ROOT


def test_find_repo_root_fails_outside_a_repository(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        find_repo_root(tmp_path)


# ── import-friends ────────────────────────────────────────────────────────────────────────────


def test_the_template_imports_to_nothing() -> None:
    template = (DATASET_DIR / "friends-template.csv").read_text("utf-8")
    assert template.startswith(HEADER)
    assert import_friends(template, TODAY, BUSINESSES) == FriendDrafts(
        rows=[], comments=0, dropped_reserved=0, skipped_examples=1
    )


def test_friends_rows_become_incomplete_drafts_with_the_question_verbatim() -> None:
    text = (
        "﻿"
        + HEADER
        + (
            'paws-and-co,hinglish,"  TEST-ONLY, with ""quotes"" and a typo teh  ",'
            "TEST-ONLY comment\n"
            "sunrise-clinic,HI,TEST-ONLY नमस्ते,\n"
            ",,,\n"
            "sunrise-clinic,en,EXAMPLE-ONLY: replace this row,\n"
        )
    )
    drafts = import_friends(text, TODAY, BUSINESSES)
    assert drafts.comments == 1  # counted, never copied (zara's review, Z1)
    assert drafts.skipped_examples == 1
    assert drafts.rows[0] == {
        "id": None,
        "replaces": None,
        "split_group": None,
        "business_id": "paws-and-co",
        "question": 'TEST-ONLY, with "quotes" and a typo teh',
        "language": "hi",
        "code_mixed": True,
        "category": None,
        "subtype": None,
        "author_type": "friend_blind",
        "saw_documents": False,
        "expected_outcome": None,
        "expected_evidence": [],
        "reference_answer": None,
        "split": None,
        "notes": None,
        "created": "2026-10-23",
        "synthetic": True,
    }
    assert (drafts.rows[1]["language"], drafts.rows[1]["code_mixed"]) == ("hi", False)
    assert len(drafts.rows) == 2
    assert "TEST-ONLY comment" not in json.dumps(drafts.rows)


TRIAGED: Final = HEADER.replace("comment\n", "comment,category\n")


def test_shesha_may_triage_and_action_or_document_rows_are_dropped_and_counted() -> None:
    # Decided 2026-10-22, item 5: action and document are refused until M5 and M6; the rows
    # are not imported, only counted, so the README can say how many were left out.
    text = TRIAGED + (
        "sunrise-clinic,en,TEST-ONLY 1,,faq\n"
        "paws-and-co,en,TEST-ONLY 2,,action\n"
        "northwind-shop,xx,TEST-ONLY 3,,Document\n"  # dropped before any other check
        "sunrise-clinic,en,TEST-ONLY 4,,\n"
    )
    drafts = import_friends(text, TODAY, BUSINESSES)
    assert [(r["question"], r["category"]) for r in drafts.rows] == [
        ("TEST-ONLY 1", "faq"),
        ("TEST-ONLY 4", None),
    ]
    assert (drafts.dropped_reserved, drafts.skipped_examples) == (2, 0)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("business,language,question\n", "the header must be exactly"),
        (HEADER.replace("comment", "comment,notes"), "the header must be exactly"),
        (TRIAGED + "sunrise-clinic,en,TEST-ONLY,,smalltalk\n", "row 2: category must be blank"),
        (HEADER + "northwind-shop,en,TEST-ONLY,\n", "row 2: business_id must be one of"),
        (HEADER + "sunrise-clinic,telugu,TEST-ONLY,\n", "row 2: language must be one of"),
        (
            HEADER + "sunrise-clinic,en,TEST-ONLY,\npaws-and-co,en,TEST-ONLY 123-456-7890,\n",
            r"^row 3: question looks like it holds personal data \(a phone-like number",
        ),
    ],
)
def test_a_bad_friends_file_names_the_row_and_column(text: str, problem: str) -> None:
    with pytest.raises(DatasetError, match=problem) as caught:
        import_friends(text, TODAY, BUSINESSES)
    assert "123-456" not in str(caught.value)


def test_import_friends_prints_jsonl(
    dataset: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv_file = tmp_path / "friend.csv"
    csv_file.write_text(HEADER + "sunrise-clinic,en,TEST-ONLY 1,TEST-ONLY comment\n", "utf-8")
    code, out = run(dataset, "import-friends", str(csv_file), "--today", "2026-10-23")
    assert code == 0
    assert json.loads(out)["question"] == "TEST-ONLY 1"
    err = capsys.readouterr().err
    assert "1 draft row(s)" in err
    assert "0 row(s) triaged as action or document were dropped" in err
    assert "0 row(s) marked EXAMPLE-ONLY were skipped" in err
    assert "1 row(s) had a comment: read them in the CSV" in err
    assert "TEST-ONLY comment" not in out + err

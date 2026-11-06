"""M4 PR 1: `report`'s counts per intent, business, language group, file and author."""

from typing import Final

from tests.intenteval.helpers import dataset, eval_row, train_row
from vectapilot_api.intenteval.report import render_report
from vectapilot_api.intents.taxonomy import DECISION_ORDER

MARKER: Final = "TEST-ONLY never-in-a-report 9e2b"


def section(report: str, title: str) -> list[str]:
    """The lines of the block that starts with `title`, up to the next blank line."""
    lines = report.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(title))
    end = next((i for i in range(start, len(lines)) if not lines[i]), len(lines))
    return lines[start:end]


def table(block: list[str], header: str) -> dict[str, list[str]]:
    """Intent (or `total`) -> the cells of one table in `block` whose header starts `header`."""
    start = next(i for i, line in enumerate(block) if line.strip().startswith(header))
    rows = block[start + 1 : start + 1 + len(DECISION_ORDER) + 1]
    return {line.split()[0]: line.split()[1:] for line in rows}


def test_an_empty_version_lists_every_cell_with_zeros() -> None:
    report = render_report(dataset())
    assert report.startswith("intents v0.1 (collecting, synthetic): 0 train rows, 0 eval rows\n")
    block = section(report, "train.jsonl · author claude")
    assert block[1].split() == ["sunrise-clinic", "en", "hi", "hi-code-mixed", "total"]
    # Paws & Co lists en and te: M4 has no Telugu rows, so only en is a column.
    sunrise = table(block, "sunrise-clinic")
    paws = table(block, "paws-and-co")
    assert list(sunrise) == [*DECISION_ORDER, "total"]
    assert all(cells == ["0", "0", "0", "0"] for cells in sunrise.values())
    assert all(cells == ["0", "0"] for cells in paws.values())
    assert "eval.jsonl · author shesha: 0 rows" in report
    assert "eval.jsonl · author friend: 0 rows" in report
    assert "train.jsonl matches train_draft_sha256: not recorded yet (PR 2 records it)" in report
    assert "verification-sample.txt: none yet" in report


def test_rows_land_in_their_file_author_business_and_language_cells() -> None:
    train = [
        train_row(id="example-t1", intent="question", verified=True),
        train_row(id="example-t2", intent="question"),
        train_row(id="example-t3", intent="complaint", business_id="paws-and-co"),
    ]
    evals = [
        eval_row(id="example-e1", intent="emergency", language="hi", code_mixed=True),
        eval_row(id="example-e2", intent="emergency", language="hi", author="friend"),
        eval_row(id="example-e3", intent="out_of_scope", split="test", steering=True),
        eval_row(id="example-e4", intent="out_of_scope", text=MARKER),
    ]
    report = render_report(dataset(train=train, eval_rows=evals, sample=("example-t1",)))
    claude = section(report, "train.jsonl · author claude")
    assert claude[0] == "train.jsonl · author claude: 3 rows"
    assert table(claude, "sunrise-clinic")["question"] == ["2", "0", "0", "2"]
    assert table(claude, "paws-and-co")["complaint"] == ["1", "1"]
    shesha = section(report, "eval.jsonl · author shesha")
    assert table(shesha, "sunrise-clinic")["emergency"] == ["0", "0", "1", "1"]
    assert table(shesha, "sunrise-clinic")["out_of_scope"] == ["2", "0", "0", "2"]
    friend = section(report, "eval.jsonl · author friend")
    assert table(friend, "sunrise-clinic")["emergency"] == ["0", "1", "0", "1"]
    splits = section(report, "eval.jsonl splits")
    assert table(splits, "intent")["out_of_scope"] == ["0", "1", "1", "2"]
    assert table(splits, "intent")["total"] == ["0", "1", "3", "4"]
    assert "steering rows (eval.jsonl): 1" in report
    assert "verification (D3): 1 of 3 train rows verified" in report
    assert "verification-sample.txt: 1 ids (1 of 3 train rows), 0 not verified yet" in report
    assert MARKER not in report


def test_the_minimums_show_counts_and_whether_each_is_met() -> None:
    evals = [eval_row(id=f"example-e{k}", intent="emergency") for k in range(10)]
    block = section(render_report(dataset(eval_rows=evals)), "minimums")
    lines = {line[:68].strip(): line[68:].split() for line in block[1:]}
    assert lines["business sunrise-clinic: eval rows of emergency"] == ["10", "of", "10", "ok"]
    assert lines["business paws-and-co: eval rows of emergency"] == ["0", "of", "10", "short"]
    assert lines["eval rows"] == ["10", "of", "160", "short"]


def test_the_report_says_whether_train_still_matches_the_recorded_draft() -> None:
    same = dataset(train_draft_sha256="a" * 64, train_sha256="a" * 64)
    other = dataset(train_draft_sha256="a" * 64, train_sha256="b" * 64)
    assert "train.jsonl matches train_draft_sha256: yes" in render_report(same)
    assert (
        "train.jsonl matches train_draft_sha256: no: train.jsonl changed since the draft was "
        "recorded"
    ) in render_report(other)

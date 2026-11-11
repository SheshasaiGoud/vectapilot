"""The command line (`tooleval/cli.py`): the dataset commands on a throwaway repository, and the
argument refusals that come before anything is read. Unit."""

import io
from pathlib import Path

import pytest

from tests.tooleval.helpers import make_repo, scenario, write_scenarios
from vectapilot_api.tooleval.cli import main
from vectapilot_api.tooleval.scenarios import FREEZE_FILE, MANIFEST_FILE, SCENARIOS_FILE


def run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    code = main(argv, out)
    return code, out.getvalue()


def test_check_and_report_on_an_empty_version(tmp_path: Path) -> None:
    dataset_dir = make_repo(tmp_path)
    code, text = run(["--dataset", str(dataset_dir), "check"])
    assert (code, text.strip()) == (0, "check (collecting, 0 scenarios): ok")
    code, text = run(["--dataset", str(dataset_dir), "report"])
    assert code == 0
    assert "scenarios                         " in text
    assert "0 of 40   short" in text
    # Decided 2026-11-11: each business's emergencies, and Sunrise's in Hindi or Hinglish (only a
    # business that allows hi has that line); the column fits the longest label.
    tail = text.split("minimums (", 1)[1].splitlines()[1:]
    rows = {line[: line.rindex(" of ")].rsplit(None, 1)[0].strip(): line for line in tail}
    assert rows["business sunrise-clinic: emergency scenarios"].endswith("0 of 2    short")
    assert rows["business paws-and-co: emergency scenarios"].endswith("0 of 2    short")
    hindi = "business sunrise-clinic: emergency scenarios in hi + hi-code-mixed"
    assert rows[hindi].endswith("0 of 1    short")
    assert "business paws-and-co: emergency scenarios in hi" not in text
    columns = {line.index(" of ") for line in rows.values()}
    assert len(columns) == 1, columns


def test_check_refuses_placeholders_and_names_only_ids(tmp_path: Path) -> None:
    dataset_dir = make_repo(tmp_path)
    write_scenarios(dataset_dir, [scenario("s-1")])
    code, text = run(["--dataset", str(dataset_dir), "check"])
    assert code == 1
    assert "PROBLEM  scenarios.jsonl scenario s-1: is a placeholder" in text
    assert "TEST-ONLY " not in text.replace("such as EXAMPLE-ONLY", "")


def test_an_unreadable_file_is_exit_2(tmp_path: Path) -> None:
    dataset_dir = make_repo(tmp_path)
    (dataset_dir / SCENARIOS_FILE).write_text("{not json\n", "utf-8")
    code, text = run(["--dataset", str(dataset_dir), "check"])
    assert code == 2
    assert text.startswith("error: scenarios.jsonl line 1 is not a valid scenario")


def test_freeze_below_the_minimums_writes_nothing(tmp_path: Path) -> None:
    dataset_dir = make_repo(tmp_path)
    before = {n: (dataset_dir / n).read_bytes() for n in (MANIFEST_FILE, SCENARIOS_FILE)}
    code, text = run(["--dataset", str(dataset_dir), "freeze"])
    assert code == 1
    assert "freeze refused: nothing was written" in text
    assert not (dataset_dir / FREEZE_FILE).exists()
    assert {n: (dataset_dir / n).read_bytes() for n in before} == before


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["run", "--split", "dev", "--live"], "--live requires --max-usd"),
        (["run", "--split", "dev", "--max-usd", "0.50"], "--max-usd is for --live runs only"),
        (["run", "--split", "dev", "--live", "--max-usd", "1.01"], "at most"),
        (["run", "--split", "dev", "--live", "--max-usd", "1e-1"], "plain digits"),
        (
            ["run", "--split", "dev", "--live", "--max-usd", "0.5", "--plans", "x.json"],
            "--plans is for dry runs only",
        ),
        (["run", "--split", "test", "--test-rerun-reason", "Not A Slug"], "lowercase slug"),
    ],
)
def test_argument_refusals_come_first(
    argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(argv)
    assert stopped.value.code == 2
    assert message in capsys.readouterr().err

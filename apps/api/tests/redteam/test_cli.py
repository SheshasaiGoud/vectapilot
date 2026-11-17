"""M7 PR 2: `python -m vectapilot_api.redteam check | report | freeze | run | summarize`
(`redteam/cli.py`), on a TEST-ONLY dataset in tmp_path.

`check` prints problems naming ids and fields, never a value; `report --counts` prints codes and
counts only. `run` refuses an unfrozen version (a dry run is `tests/redteam/test_runs.py`'s),
and `--live` is refused in PR 2 whatever its cap (agents never pass it; these tests check the
refusal only).
"""

import io
from pathlib import Path

import pytest

from tests.redteam.helpers import case, g_case, write_corpus, write_dataset
from vectapilot_api.redteam.cli import main
from vectapilot_api.redteam.pii import PiiGenerator, PiiType, Placeholder


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out=out)
    return code, out.getvalue()


def dataset_args(tmp_path: Path, cases: list[dict[str, object]]) -> list[str]:
    corpus = write_corpus(tmp_path)
    dataset = write_dataset(tmp_path, cases)
    return ["--dataset", str(dataset), "--corpus", str(corpus)]


def test_check_ok_and_with_a_problem(tmp_path: Path) -> None:
    args = dataset_args(tmp_path, [case(), g_case()])
    assert run(*args, "check") == (0, "check (collecting, 2 cases): ok\n")
    value = PiiGenerator().value("l-001", Placeholder(PiiType.PAN, 1))
    bad = dataset_args(tmp_path / "bad", [case(input=f"my PAN is {value}")])
    code, out = run(*bad, "check")
    assert code == 1
    assert out == (
        "PROBLEM  case l-001 input: a PAN-shaped code (write {{pii:<type>}})\n"
        "check (collecting, 1 cases): 1 problem(s)\n"
    )
    assert value not in out


def test_an_unreadable_dataset_exits_2(tmp_path: Path) -> None:
    code, out = run("--dataset", str(tmp_path), "check")
    assert code == 2 and out.startswith("error: manifest.json is missing")


def test_report_counts_hold_codes_and_counts_only(tmp_path: Path) -> None:
    marker = "TEST-ONLY-INPUT-TEXT"
    args = dataset_args(tmp_path, [case(input=marker), g_case(language="hi", code_mixed=True)])
    code, out = run(*args, "report", "--counts")
    assert code == 0 and marker not in out
    assert out.startswith("redteam v0.1 (collecting): 2 cases, 2 families, 0 reviewed by a person")
    assert "  hi-code-mixed" in out and "  G-in" in out and "cases:" not in out
    code, full = run(*args, "report")
    assert code == 0 and marker not in full and "  l-001  L " in full


def test_freeze_then_refreeze_is_reported(tmp_path: Path) -> None:
    args = dataset_args(tmp_path, [case()])
    code, out = run(*args, "freeze", "--today", "2026-11-16")
    assert code == 0 and out.startswith("frozen redteam v0.1: 1 cases.")
    code, out = run(*args, "freeze", "--today", "2026-11-16")
    assert code == 1 and out.endswith("freeze refused: nothing was written\n")
    code, out = run(*args, "freeze", "--refreeze", "--today", "2026-11-17")
    assert code == 0 and out.startswith("RE-FROZEN v0.1 (frozen before on 2026-11-16)")
    assert run(*args, "check")[0] == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--live"],
        ["run", "--max-usd", "0.50"],
        ["run", "--live", "--max-usd", "1.01"],
        ["run", "--live", "--max-usd", "0"],
        ["run", "--live", "--max-usd", "1e-1"],
    ],
)
def test_run_arguments_are_refused_before_anything_is_read(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(argv)
    assert stopped.value.code == 2


def test_live_is_refused_in_pr_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--live", "--max-usd", "0.50"]) == 1
    assert "--live is refused: D7's raw-arm live run is built in the close-out" in (
        capsys.readouterr().err
    )


def test_run_refuses_a_version_that_is_not_frozen(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = dataset_args(tmp_path, [case()])

    assert main([*args, "run"]) == 1
    assert "v0.1 is not frozen: run `python -m vectapilot_api.redteam freeze` first" in (
        capsys.readouterr().err
    )


def test_summarize_refuses_a_folder_without_a_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["summarize", str(tmp_path)]) == 1
    assert "redteam:" in capsys.readouterr().err

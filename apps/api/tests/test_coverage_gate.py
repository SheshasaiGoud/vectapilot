"""The gateway coverage gate's decisions (scripts/coverage_gate.py, ADR 0006)."""

import os
import tomllib
from pathlib import Path

import pytest

from scripts.coverage_gate import (
    Counts,
    branch_only_failure,
    combined_failure,
    counts_from_totals,
    leftover_data_files,
    main,
    pragma_lines,
    unreported_files,
)

API_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("covered", "total"), [(19, 20), (95, 100), (54, 54), (1, 1)])
def test_branch_only_passes_at_or_above_95_percent(covered: int, total: int) -> None:
    assert branch_only_failure(covered, total) is None


@pytest.mark.parametrize(
    ("covered", "total"),
    [
        (18, 20),  # 90 %
        (94, 100),
        (0, 54),
        # 94.9995 %: rounded to two decimals this shows as "95.00", but it is below 95 %.
        (189_999, 200_000),
    ],
)
def test_branch_only_fails_below_95_percent(covered: int, total: int) -> None:
    failure = branch_only_failure(covered, total)

    assert failure is not None
    assert f"{covered}/{total}" in failure


def test_zero_branches_fails() -> None:
    failure = branch_only_failure(0, 0)

    assert failure is not None
    assert "no branches" in failure


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(0, None), (2, "combined total below 95.00 %"), (1, "`coverage report` failed (exit 1")],
)
def test_combined_failure_tells_a_low_total_from_an_error(
    returncode: int, expected: str | None
) -> None:
    failure = combined_failure(returncode)

    if expected is None:
        assert failure is None
    else:
        assert failure is not None
        assert failure.startswith(expected)


def test_counts_are_read_from_the_json_totals() -> None:
    totals = {"covered_lines": 9, "num_statements": 10, "covered_branches": 3, "num_branches": 4}

    assert counts_from_totals(totals) == Counts(9, 10, 3, 4)


def test_a_report_without_branch_data_reads_as_zero_branches_and_fails() -> None:
    # `coverage json` leaves out every branch key when `branch = true` is missing.
    counts = counts_from_totals({"covered_lines": 10, "num_statements": 10})

    assert counts.branches == 0
    assert branch_only_failure(counts.covered_branches, counts.branches) is not None


def test_leftover_parallel_data_files_are_found(tmp_path: Path) -> None:
    (tmp_path / ".coverage").touch()
    assert leftover_data_files(tmp_path / ".coverage") == []

    leftover = tmp_path / ".coverage.host.1234.XyZ"
    leftover.touch()
    assert leftover_data_files(tmp_path / ".coverage") == [leftover]


def test_files_missing_from_the_report_are_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = tmp_path / "gateway"
    (gateway / "no_init_dir").mkdir(parents=True)
    (gateway / "measured.py").touch()
    (gateway / "no_init_dir" / "router.py").touch()
    monkeypatch.chdir(tmp_path)  # coverage.py names files relative to the current directory

    assert unreported_files(gateway, ["gateway/measured.py"]) == ["gateway/no_init_dir/router.py"]


def test_exclusion_comments_are_found(tmp_path: Path) -> None:
    gateway = tmp_path / "gateway"
    (gateway / "sub").mkdir(parents=True)
    (gateway / "a.py").write_text(
        "x = 1\nif x:  # pragma: no cover\n    y = 2\n"  # line 2
        "def f():  #PRAGMA:NO BRANCH\n    pass\n",  # line 4, coverage.py accepts both spellings
        encoding="utf-8",
    )
    (gateway / "sub" / "b.py").write_text("z = 3  # pragma no branch\n", encoding="utf-8")
    (gateway / "clean.py").write_text("def g() -> None: ...\n", encoding="utf-8")

    assert pragma_lines(gateway) == [
        "gateway/a.py:2: if x:  # pragma: no cover",
        "gateway/a.py:4: def f():  #PRAGMA:NO BRANCH",
        "gateway/sub/b.py:1: z = 3  # pragma no branch",
    ]


def test_the_gate_fails_without_coverage_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    # The gate's `coverage` subprocesses read this data file, which does not exist.
    monkeypatch.setenv("COVERAGE_FILE", str(tmp_path / "missing.coverage"))

    assert main() == 1
    assert "`coverage json` failed" in capfd.readouterr().out


def test_the_gate_stops_on_a_leftover_parallel_data_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COVERAGE_FILE", str(tmp_path / ".coverage"))
    leftover = tmp_path / ".coverage.other-machine.99.AbC"
    leftover.touch()

    assert main() == 1
    assert "parallel coverage data files found" in capfd.readouterr().out
    assert leftover.exists()  # the gate stopped before any `coverage` command could merge it


def test_coverage_settings_are_pinned() -> None:
    # Any other setting (omit, exclude_also, exclude_lines, partial_branches, fail_under, ...)
    # can shrink or bend what the gate measures, so adding one must be a deliberate, reviewed
    # change here too (ADR 0006).
    pyproject = tomllib.loads((API_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["tool"]["coverage"] == {
        "run": {"branch": True, "source_pkgs": ["vectapilot_api"]},
        "report": {"precision": 2, "include_namespace_packages": True},
    }


def test_no_other_file_configures_coverage() -> None:
    # coverage.py reads the first of these that holds coverage settings and then ignores
    # pyproject.toml; COVERAGE_RCFILE overrides them all.
    for name in (".coveragerc", ".coveragerc.toml", "setup.cfg", "tox.ini"):
        assert not (API_ROOT / name).exists(), name
    assert "COVERAGE_RCFILE" not in os.environ

"""Coverage gate for the LLM gateway (ADR 0006). Run it after the tests ran under coverage:

    uv run coverage run -m pytest            # 1. run the tests, recording coverage in `.coverage`
    uv run python scripts/coverage_gate.py   # 2. this script: check the gateway's numbers

`make coverage` runs both lines; the CI `api` job runs the same two commands. The gate does not know
whether the tests passed: `make coverage` and CI run it only after a green `coverage run`. Do not
use it on its own as a gate.

Both checks read the same files (`INCLUDE`) and both must pass:

1. Combined: coverage.py's own total (statements and branches together) is at least 95.00 %.
   `coverage report --fail-under` decides; it also prints the per-file table with what was missed.
2. Branch-only: the playbook asks for 95 % *branch* coverage, and many fully run statements can
   hide missed branches in the combined total. The branch counts come from `coverage json` and are
   compared as whole numbers, 100 * covered >= 95 * total, so no float rounding decides. Zero
   branches fails too: a measurement that saw no branch (for example `branch = true` missing from
   pyproject.toml) proves nothing.

Code can also leave the measurement quietly, which would make both numbers look better. Three
guards fail closed on that:

- Every `*.py` under gateway/ must be in the report. This catches an `omit` setting and a
  directory without `__init__.py`. pyproject.toml's `source_pkgs` and `include_namespace_packages`
  make a module that no test imports count at 0 %.
- No `pragma: no cover` / `pragma: no branch` comment in gateway/. coverage.py honours both
  without any configuration. ADR 0006 allows one only with an ADR note, and that note must also
  change this check.
- No leftover parallel data file (`.coverage.*`): `coverage report` would merge it into this run.

tests/test_coverage_gate.py pins the coverage settings themselves. Exit code: 0 if everything
passes, 1 otherwise.
"""

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final, NamedTuple

INCLUDE: Final = "*/vectapilot_api/gateway/*"
MIN_PERCENT: Final = 95
GATEWAY_DIR: Final = Path(__file__).resolve().parents[1] / "src" / "vectapilot_api" / "gateway"
# coverage.py's two built-in exclusion comments, joined (coverage/config.py: the first entries of
# DEFAULT_EXCLUDE and DEFAULT_PARTIAL). Its other defaults (`...` bodies, `if TYPE_CHECKING:`) skip
# only code that never runs, so they stay allowed.
PRAGMA: Final = re.compile(r"#\s*(pragma|PRAGMA)[:\s]?\s*(no|NO)\s*(cover|COVER|branch|BRANCH)")


class Counts(NamedTuple):
    """What the gateway's tests reached, as "covered out of total"."""

    covered_statements: int
    statements: int
    covered_branches: int
    branches: int


def counts_from_totals(totals: dict[str, Any]) -> Counts:
    """Read the counts from the `totals` of a `coverage json` report.

    Without branch measurement the report has no branch keys at all; that reads as 0 branches,
    which the branch-only check then fails.
    """
    return Counts(
        covered_statements=int(totals["covered_lines"]),
        statements=int(totals["num_statements"]),
        covered_branches=int(totals.get("covered_branches", 0)),
        branches=int(totals.get("num_branches", 0)),
    )


def combined_failure(returncode: int) -> str | None:
    """Explain the exit code of `coverage report --fail-under`, or None if it passed."""
    if returncode == 0:
        return None
    if returncode == 2:  # coverage.py's exit code for "total below --fail-under"
        return f"combined total below {MIN_PERCENT}.00 %"
    return f"`coverage report` failed (exit {returncode}; see its message above)"


def branch_only_failure(covered: int, total: int, min_percent: int = MIN_PERCENT) -> str | None:
    """Return why the branch-only check fails, or None if it passes. Whole numbers only."""
    if total == 0:
        return "no branches were measured (is `branch = true` set?); an empty measurement fails"
    if 100 * covered < min_percent * total:
        return f"branch-only coverage {covered}/{total} is below {min_percent} %"
    return None


def leftover_data_files(data_file: Path) -> list[Path]:
    """Parallel data files (`.coverage.*`) next to the data file.

    `coverage report` would merge them into this run's data, then delete them, so a stale file
    could make the gate pass and leave no trace.
    """
    return sorted(data_file.parent.glob(f"{data_file.name}.*"))


def unreported_files(gateway_dir: Path, reported: Iterable[str]) -> list[str]:
    """Gateway files on disk that the coverage report leaves out.

    `reported` holds the report's file names, relative to the current directory (as coverage.py
    writes them) or absolute.
    """
    in_report = {Path(name).resolve() for name in reported}
    return [
        str(path.relative_to(gateway_dir.parent))
        for path in sorted(gateway_dir.rglob("*.py"))
        if path.resolve() not in in_report
    ]


def pragma_lines(gateway_dir: Path) -> list[str]:
    """Every gateway line with a comment that makes coverage.py skip code, as `file:line: text`."""
    hits: list[str] = []
    for path in sorted(gateway_dir.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if PRAGMA.search(line):
                hits.append(f"{path.relative_to(gateway_dir.parent)}:{number}: {line.strip()}")
    return hits


def run_coverage(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run `coverage <args>` with this script's Python, so the same coverage reads the data."""
    command = [sys.executable, "-m", "coverage", *args]
    # S603 asks whether the arguments can be trusted: they are fixed here, and no shell is used.
    return subprocess.run(command, capture_output=capture, text=True, check=False)  # noqa: S603


def main() -> int:
    # Before any `coverage` command: reporting would merge (and delete) leftover parallel files.
    leftovers = leftover_data_files(Path(os.environ.get("COVERAGE_FILE", ".coverage")))
    if leftovers:
        names = ", ".join(str(path) for path in leftovers)
        print(f"coverage gate: FAILED — parallel coverage data files found ({names});")
        print("delete them, or combine them on purpose (`coverage combine`), then run again")
        return 1

    # Check 1. Not captured: coverage's table and its own failure line go straight to the log.
    report = run_coverage(
        "report", f"--include={INCLUDE}", f"--fail-under={MIN_PERCENT}", "--show-missing"
    )

    # Check 2 needs the raw counts: `-o -` writes the JSON report to stdout instead of a file.
    # `--fail-under=0` keeps any `fail_under` setting from changing this command's exit code.
    as_json = run_coverage(
        "json", "-q", "-o", "-", f"--include={INCLUDE}", "--fail-under=0", capture=True
    )
    if as_json.returncode != 0:
        print(as_json.stdout + as_json.stderr, end="")
        print(f"coverage gate: FAILED — `coverage json` failed (exit {as_json.returncode}).")
        print("No gateway numbers: no data file (run `coverage run -m pytest` first), or")
        print(f"--include={INCLUDE} matched no files")
        return 1
    report_json: dict[str, Any] = json.loads(as_json.stdout)
    counts = counts_from_totals(report_json["totals"])

    combined = report_json["totals"]["percent_covered_display"]  # rounded to `precision`
    print()
    print(f"gateway statements: {counts.covered_statements}/{counts.statements}")
    print(f"gateway branches:   {counts.covered_branches}/{counts.branches}")
    print(f"combined:           {combined} % (gate: >= {MIN_PERCENT}.00 %)")
    if counts.branches:
        branch_percent = 100 * counts.covered_branches / counts.branches  # display only
        print(f"branch-only:        {branch_percent:.2f} % (gate: >= {MIN_PERCENT} %)")

    branch_problem = branch_only_failure(counts.covered_branches, counts.branches)
    problems = (combined_failure(report.returncode), branch_problem)
    failures = [problem for problem in problems if problem is not None]
    failures += [
        f"{name} is not in the report (an `omit` setting? a missing __init__.py?)"
        for name in unreported_files(GATEWAY_DIR, report_json["files"])
    ]
    failures += [
        f"exclusion comment (ADR 0006: needs an ADR note): {hit}"
        for hit in pragma_lines(GATEWAY_DIR)
    ]

    if failures:
        print("coverage gate: FAILED")
        for failure in failures:
            print(f"  - {failure}")
        # Only a threshold miss is fixed by more tests; a setup or config problem is not.
        below_threshold = report.returncode == 2 or (
            branch_problem is not None and counts.branches > 0
        )
        if below_threshold:
            print("Add tests for the lines and branches listed under Missing above.")
        return 1
    print("coverage gate: passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

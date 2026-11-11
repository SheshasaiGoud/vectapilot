"""CI gate for the `mcp` job: fail unless the integration tests really ran.

    uv run pytest --junit-xml=pytest.xml                   # 1. the tests, with pytest's report
    uv run python scripts/integration_gate.py pytest.xml   # 2. this script

The tests that prove the server against Postgres (tests/integration/: the owner DSN refused,
another business's rows invisible, a proposal that stores nothing) skip without
`RUN_INTEGRATION=1`, and a skipped test passes. So, as apps/api's canary gate, this reads pytest's
own JUnit report and fails closed unless at least one test under tests/integration/ passed and
none of them was skipped (an xfail counts as skipped here: there is no pending hand-write in this
package). Failures need no check: the test step has already failed the job.

Standalone on purpose: it imports nothing from the project (tests import it instead).

Exit code: 0 if the integration tests ran, 1 otherwise.
"""

import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

SUITE: Final = "tests.integration."
"""JUnit `classname` prefix of the integration tests (their module path)."""
NOT_PASSED: Final = ("failure", "error", "skipped")


@dataclass
class SuiteCounts:
    passed: int = 0
    not_passed: list[str] = field(default_factory=list)


def suite_counts(report: str) -> SuiteCounts:
    """Count the suite's passed and not-passed tests in a pytest JUnit XML report."""
    # S314: the report comes from pytest in the same CI job, not from outside.
    root = ElementTree.fromstring(report)  # noqa: S314
    counts = SuiteCounts()
    for case in root.iter("testcase"):
        if not case.get("classname", "").startswith(SUITE):
            continue
        if any(case.find(tag) is not None for tag in NOT_PASSED):
            counts.not_passed.append(f"{case.get('classname')}::{case.get('name')}")
        else:
            counts.passed += 1
    return counts


def problems(counts: SuiteCounts) -> list[str]:
    found: list[str] = []
    if counts.passed == 0:
        found.append("no integration test passed (did RUN_INTEGRATION=1 reach pytest?)")
    if counts.not_passed:
        found.append(f"integration tests did not pass: {', '.join(counts.not_passed)}")
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: integration_gate.py <pytest-junit.xml>", file=sys.stderr)
        return 1
    counts = suite_counts(Path(argv[1]).read_text(encoding="utf-8"))
    found = problems(counts)
    for problem in found:
        print(f"integration gate: {problem}", file=sys.stderr)
    if not found:
        print(f"integration gate: {counts.passed} integration tests passed")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

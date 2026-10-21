"""CI gate for the canary leakage suite (ADR 0009): fail unless the suite really ran.

    uv run coverage run -m pytest --junit-xml=pytest.xml   # 1. the tests, with pytest's report
    uv run python scripts/canary_gate.py pytest.xml        # 2. this script

Zero cross-business leaks is a hard gate (business-context §6). A suite that was skipped,
deselected, moved or renamed reports no leak either, so this reads pytest's own JUnit report and
fails closed unless at least one test under tests/canary/ passed and none of them was skipped.
An expected failure (`xfail`) is a recorded, documented gap and is allowed. Failures need no check
here: the test step has already failed the job.

Exit code: 0 if the suite ran, 1 otherwise.
"""

import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

SUITE: Final = "tests.canary."
"""JUnit `classname` prefix of the suite's tests (their module path)."""

NOT_PASSED: Final = ("failure", "error", "skipped")


@dataclass
class SuiteCounts:
    passed: int = 0
    skipped: list[str] = field(default_factory=list)  # plain skips, not xfail


def suite_counts(report: str) -> SuiteCounts:
    """Count the suite's passed and skipped tests in a pytest JUnit XML report."""
    # S314: the report comes from pytest in the same CI job, not from outside.
    root = ElementTree.fromstring(report)  # noqa: S314
    counts = SuiteCounts()
    for case in root.iter("testcase"):
        if not case.get("classname", "").startswith(SUITE):
            continue
        outcomes = [child for child in case if child.tag in NOT_PASSED]
        if not outcomes:
            counts.passed += 1
        elif any(c.tag == "skipped" and c.get("type") != "pytest.xfail" for c in outcomes):
            counts.skipped.append(f"{case.get('classname')}::{case.get('name')}")
    return counts


def gate_problems(counts: SuiteCounts) -> list[str]:
    problems: list[str] = []
    if counts.passed == 0:
        problems.append(f"no canary test passed (none under {SUITE!r} in the report)")
    problems.extend(f"skipped: {name}" for name in counts.skipped)
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: canary_gate.py <pytest-junit.xml>", file=sys.stderr)
        return 1
    try:
        counts = suite_counts(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, ElementTree.ParseError) as error:
        print(f"canary gate: cannot read the report: {error}", file=sys.stderr)
        return 1
    problems = gate_problems(counts)
    for problem in problems:
        print(f"canary gate: {problem}", file=sys.stderr)
    if not problems:
        print(f"canary gate: {counts.passed} canary tests passed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

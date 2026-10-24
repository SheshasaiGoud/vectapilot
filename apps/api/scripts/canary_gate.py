"""CI gate for the canary leakage suite (ADR 0009): fail unless the suite really ran.

    uv run coverage run -m pytest --junit-xml=pytest.xml   # 1. the tests, with pytest's report
    uv run python scripts/canary_gate.py pytest.xml        # 2. this script

Zero cross-business leaks is a hard gate (business-context §6). A suite that was skipped,
deselected, moved or renamed reports no leak either, so this reads pytest's own JUnit report and
fails closed unless at least one test under tests/canary/ passed and none of them was skipped.
Failures need no check here: the test step has already failed the job.

**Expected failures (M3 PR 3, ADR 0009 amendment).** An `xfail` under tests/canary/ is allowed
only when it is a pending `[HAND-WRITE]` stub's mark: `tests/canary/read_paths.py`'s
`pending_hand_writes` builds its reason with `pending_reason` below, and this gate accepts exactly
that reason and nothing else. Any other xfail (`reason="flaky in CI"`, no reason, a near miss) is
refused: an xfail on a canary test would turn a leak into a quiet "expected failure". There is no
allowlist of known gaps (D10 (a)).

pytest's report keeps only an xfail's reason, not its `strict` or `raises`, so this gate cannot
see a copied reason on a hand-made mark. `tests/test_canary_checks.py` closes that: the only
`xfail` anywhere in tests/canary/ is the one inside `pending_hand_writes`.

Standalone on purpose: run by path, Python does not put apps/api on the import path, so this file
imports nothing from the project (tests import it instead).

Exit code: 0 if the suite ran, 1 otherwise.
"""

import re
import sys
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

SUITE: Final = "tests.canary."
"""JUnit `classname` prefix of the suite's tests (their module path)."""

NOT_PASSED: Final = ("failure", "error", "skipped")
XFAIL: Final = "pytest.xfail"
"""The `type` pytest gives the `<skipped>` element of an expected failure."""

PENDING_PREFIX: Final = "[HAND-WRITE] "
PENDING_SUFFIX: Final = " not written yet (tests/canary/read_paths.py, PENDING_HAND_WRITE)"
_STUB: Final = r"[a-z_][a-z0-9_]*"
_PENDING: Final = re.compile(
    rf"{re.escape(PENDING_PREFIX)}(?P<stubs>{_STUB}(?:, {_STUB})*){re.escape(PENDING_SUFFIX)}"
)


def pending_reason(stubs: Iterable[str]) -> str:
    """The xfail reason of a test that runs the pending [HAND-WRITE] `stubs` (at least one)."""
    names = list(stubs)
    if not names or not all(re.fullmatch(_STUB, name) for name in names):
        raise ValueError("pending stubs are one or more Python function names")
    return f"{PENDING_PREFIX}{', '.join(names)}{PENDING_SUFFIX}"


def pending_stubs(reason: str | None) -> tuple[str, ...] | None:
    """The stubs a pending reason names, or None when `reason` is not exactly one."""
    match = _PENDING.fullmatch(reason or "")
    return None if match is None else tuple(match["stubs"].split(", "))


@dataclass
class SuiteCounts:
    passed: int = 0
    skipped: list[str] = field(default_factory=list)  # plain skips, not xfail
    pending: set[str] = field(default_factory=set)  # stubs named by allowed xfails
    xfailed: int = 0  # allowed xfails (pending [HAND-WRITE] marks)
    refused_xfails: list[str] = field(default_factory=list)  # every other xfail


def suite_counts(report: str) -> SuiteCounts:
    """Count the suite's passed, skipped and xfailed tests in a pytest JUnit XML report."""
    # S314: the report comes from pytest in the same CI job, not from outside.
    root = ElementTree.fromstring(report)  # noqa: S314
    counts = SuiteCounts()
    for case in root.iter("testcase"):
        if not case.get("classname", "").startswith(SUITE):
            continue
        name = f"{case.get('classname')}::{case.get('name')}"
        outcomes = [child for child in case if child.tag in NOT_PASSED]
        if not outcomes:
            counts.passed += 1
        for outcome in outcomes:
            if outcome.tag != "skipped":
                continue
            if outcome.get("type") != XFAIL:
                counts.skipped.append(name)
                continue
            reason = outcome.get("message")
            stubs = pending_stubs(reason)
            if stubs is None:
                counts.refused_xfails.append(f"{name} (reason: {reason!r})")
            else:
                counts.xfailed += 1
                counts.pending.update(stubs)
    return counts


def gate_problems(counts: SuiteCounts) -> list[str]:
    problems: list[str] = []
    if counts.passed == 0:
        problems.append(f"no canary test passed (none under {SUITE!r} in the report)")
    problems.extend(f"skipped: {name}" for name in counts.skipped)
    problems.extend(
        f"xfail that is not a pending [HAND-WRITE] mark: {name}" for name in counts.refused_xfails
    )
    return problems


def summary(counts: SuiteCounts) -> str:
    line = f"canary gate: {counts.passed} canary tests passed"
    if counts.xfailed:
        stubs = ", ".join(sorted(counts.pending))
        line += f"; {counts.xfailed} xfailed on pending [HAND-WRITE] stubs ({stubs})"
    return line


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
        print(summary(counts))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

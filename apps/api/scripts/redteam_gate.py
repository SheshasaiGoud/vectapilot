"""CI gate for the frozen red-team suite (M7 PR 2; scoring rules, "The gate"; D8 (a)).

    uv run coverage run -m pytest --junit-xml=pytest.xml      # 1. the tests, with pytest's report
    uv run python scripts/redteam_gate.py pytest.xml          # 2. this script
    uv run python scripts/redteam_gate.py pytest.xml --final  # at M7's close-out (PR 9)

A suite that was skipped, deselected, filtered or edited reports no failure either, so this reads
pytest's own JUnit report of `tests/redteam/test_cases.py` (`test_case[<case id>]`) next to the
frozen dataset (`evals/datasets/redteam/v0.1/`: manifest, cases.jsonl, gates.json, freeze.json),
and fails closed on any of:
- a version that is not frozen, or a cases.jsonl whose SHA-256 is not freeze.json's;
- a frozen id missing from the report or present twice, or an id that is not frozen;
- a number of cases other than the manifest's pinned count;
- a failure or an error (the test step has already failed the job; counted again here);
- any plain skip (a skip would look like a pass);
- an xfail reason outside the closed forms: `pending:<PR n|Hn|M5-H1>` (`PENDING_REASONS`) and
  `known_gap:<its own id>` on a case gates.json labels `known_gap`;
- a hard case xfailed as a known gap, a known_gap case that passed without its mark, or a soft
  case that passed without its recorded outcome code;
- more soft over-escalations (a recorded outcome other than the case's `expect`) than
  gates.json's `max_over_escalated`;
- a gates.json that loosened against freeze.json's frozen labels: a label moved down (hard to
  soft or known_gap) or between known_gap and soft, an id added or removed, or a raised ceiling
  (noah's PR 2 review: the gate, not only a pytest check, refuses it);
- with `--final`: any pending reason but `pending:M5-H1` (D8 (i)), and the gate lists the
  `pending:M5-H1` ids as "not measured through the real loop".

pytest's report keeps an xfail's reason but not its `strict` or `raises`; `tests/redteam/
conftest.py` checks those at collection time, and `tests/test_redteam_gate.py` proves no other
file under tests/redteam/ makes an xfail.

Standalone on purpose, like `canary_gate.py`: run by path, Python does not put apps/api on the
import path, so this file imports nothing from the project (tests import it, and pin
`PENDING_REASONS` to `vectapilot_api.redteam.plan`'s).

Exit code: 0 if the gate holds, 1 otherwise. Output: ids, codes and counts, never a case's text.
"""

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ElementTree
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
DEFAULT_DATASET: Final = REPO_ROOT / "evals" / "datasets" / "redteam" / "v0.1"
SUITE_CLASS: Final = "tests.redteam.test_cases"
"""JUnit `classname` of the suite's tests (their module path)."""
TEST_NAME: Final = re.compile(r"^test_case\[(?P<id>[a-l]-[0-9]{3})\]$")
XFAIL: Final = "pytest.xfail"
"""The `type` pytest gives the `<skipped>` element of an expected failure."""

PENDING_REASONS: Final = frozenset(
    f"pending:{build}"
    for build in ("PR 3", "PR 5", "PR 6", "PR 7", "PR 8", "H1", "H2", "H3", "M5-H1")
)
REAL_LOOP_PENDING: Final = "pending:M5-H1"
FINAL_PENDING: Final = frozenset({REAL_LOOP_PENDING})
KNOWN_GAP: Final = re.compile(r"^known_gap:(?P<id>[a-l]-[0-9]{3})$")
OUTCOME: Final = re.compile(r"^[a-z][a-z0-9_]*(:[a-z_]+(,[a-z_]+)*)?$")
"""A recorded outcome code (`allowed`, `urgent_handoff`, `masked:email,phone`): never text."""


class DatasetError(Exception):
    """The frozen dataset cannot be read; the message names files and fields only."""


RANK: Final = {"known_gap": 0, "soft": 0, "hard": 1}
"""Labels only tighten: known_gap and soft rank alike, so no move between them is allowed."""


@dataclass(frozen=True)
class Frozen:
    total: int
    expects: dict[str, str]
    labels: dict[str, str]
    max_over_escalated: int
    loosened: tuple[str, ...] = ()
    """Every way gates.json loosened against freeze.json's frozen gates (`loosening`)."""


def loosening(frozen: object, labels: dict[str, str], ceiling: int) -> list[str]:
    """How gates.json (`labels`, `ceiling`) loosened against freeze.json's `gates`."""
    if not isinstance(frozen, dict):
        raise DatasetError("freeze.json has no frozen gates")
    before, top = frozen.get("labels"), frozen.get("max_over_escalated")
    if not isinstance(before, dict) or not isinstance(top, int):
        raise DatasetError("freeze.json's gates need labels and max_over_escalated")
    problems = []
    if ceiling > top:
        problems.append("gates.json raises max_over_escalated above the frozen ceiling")
    for case_id in sorted(set(before) | set(labels)):
        old, new = before.get(case_id), labels.get(case_id)
        if old is None or new is None:
            what = "added to" if old is None else "removed from"
            problems.append(f"{case_id}: label {what} gates.json after the freeze")
        elif new != old and (new not in RANK or RANK[new] <= RANK.get(old, 0)):
            problems.append(f"{case_id}: label moved from {old} to {new} (labels only tighten)")
    return problems


def _json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DatasetError(f"cannot read {path.name}: {type(error).__name__}") from None


def read_frozen(dataset: Path) -> Frozen:
    """The frozen version's ids, expectations, labels, pinned count and ceiling."""
    manifest, gates, freeze = (
        _json(dataset / name) for name in ("manifest.json", "gates.json", "freeze.json")
    )
    if not (isinstance(manifest, dict) and isinstance(gates, dict) and isinstance(freeze, dict)):
        raise DatasetError("manifest.json, gates.json and freeze.json are JSON objects")
    if manifest.get("stage") != "frozen" or not isinstance(manifest.get("counts"), dict):
        raise DatasetError("the version is not frozen (manifest.json stage and counts)")
    try:
        data = (dataset / "cases.jsonl").read_bytes()
    except OSError as error:
        raise DatasetError(f"cannot read cases.jsonl: {type(error).__name__}") from None
    if hashlib.sha256(data).hexdigest() != freeze.get("cases_sha256"):
        raise DatasetError("cases.jsonl changed after the freeze (its SHA-256 is not pinned)")
    expects: dict[str, str] = {}
    ids: list[str] = []
    for line in data.decode("utf-8").splitlines():
        if line.strip():
            case = json.loads(line)
            ids.append(case["id"])
            expects[case["id"]] = case["expect"]
    if len(set(ids)) != len(ids):
        raise DatasetError("cases.jsonl lists an id twice")
    labels, ceiling = gates.get("labels"), gates.get("max_over_escalated")
    total = manifest["counts"].get("total")
    if not isinstance(labels, dict) or not isinstance(ceiling, int) or not isinstance(total, int):
        raise DatasetError("gates.json labels and ceiling, and the manifest's total, are needed")
    loosened = tuple(loosening(freeze.get("gates"), labels, ceiling))
    return Frozen(total, expects, labels, ceiling, loosened)


@dataclass
class Report:
    seen: Counter[str] = field(default_factory=Counter)
    unknown: list[str] = field(default_factory=list)
    hard_passed: int = 0
    known_gaps: list[str] = field(default_factory=list)
    pending: dict[str, list[str]] = field(default_factory=dict)
    soft: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _outcome(case: ElementTree.Element) -> str | None:
    for prop in case.iter("property"):
        if prop.get("name") == "outcome":
            return prop.get("value")
    return None


def _xfail(report: Report, frozen: Frozen, case_id: str, reason: str | None) -> None:
    label = frozen.labels.get(case_id)
    known = KNOWN_GAP.fullmatch(reason or "")
    if reason in PENDING_REASONS:
        report.pending.setdefault(reason, []).append(case_id)
    elif known is not None and known["id"] == case_id and label == "known_gap":
        report.known_gaps.append(case_id)
    elif known is not None and known["id"] == case_id:
        report.problems.append(f"{case_id}: xfailed as a known gap but labelled {label}")
    else:
        report.problems.append(f"{case_id}: an xfail reason outside the closed forms")


def _passed(report: Report, frozen: Frozen, case_id: str, case: ElementTree.Element) -> None:
    label = frozen.labels.get(case_id)
    if label == "hard":
        report.hard_passed += 1
    elif label == "soft":
        outcome = _outcome(case)
        if outcome is None or not OUTCOME.fullmatch(outcome):
            report.problems.append(f"{case_id}: a soft case passed without its outcome code")
        else:
            report.soft[case_id] = outcome
    else:
        report.problems.append(f"{case_id}: passed, but labelled {label} (a mark is missing)")


def read_report(xml: str, frozen: Frozen) -> Report:
    """Classify every `test_case[<id>]` of the JUnit report against the frozen dataset."""
    # S314: the report comes from pytest in the same CI job, not from outside.
    root = ElementTree.fromstring(xml)  # noqa: S314
    report = Report()
    for case in root.iter("testcase"):
        if case.get("classname") != SUITE_CLASS:
            continue
        match = TEST_NAME.fullmatch(case.get("name", ""))
        if match is None:
            report.problems.append("a test in the suite's module that is not test_case[<id>]")
            continue
        case_id = match["id"]
        report.seen[case_id] += 1
        if case_id not in frozen.expects:
            report.unknown.append(case_id)
            continue
        outcomes = [child for child in case if child.tag in ("failure", "error", "skipped")]
        if not outcomes:
            _passed(report, frozen, case_id, case)
        for outcome in outcomes:
            if outcome.tag != "skipped":
                report.problems.append(f"{case_id}: {outcome.tag}")
            elif outcome.get("type") != XFAIL:
                report.problems.append(f"{case_id}: plain skip")
            else:
                _xfail(report, frozen, case_id, outcome.get("message"))
    return report


def gate_problems(report: Report, frozen: Frozen, *, final: bool) -> list[str]:
    problems = list(report.problems) + list(frozen.loosened)
    missing = sorted(set(frozen.expects) - set(report.seen))
    if missing:
        problems.append(f"frozen cases missing from the report: {', '.join(missing)}")
    twice = sorted(i for i, n in report.seen.items() if n > 1)
    if twice:
        problems.append(f"cases reported more than once: {', '.join(twice)}")
    if report.unknown:
        problems.append(f"ids that are not frozen: {', '.join(sorted(report.unknown))}")
    ran = sum(report.seen.values())
    if ran != frozen.total or len(frozen.expects) != frozen.total:
        problems.append(
            f"{ran} cases ran and cases.jsonl holds {len(frozen.expects)}; the manifest pins"
            f" {frozen.total}"
        )
    over = over_escalated(report, frozen)
    if len(over) > frozen.max_over_escalated:
        problems.append(
            f"{len(over)} soft over-escalations ({', '.join(over)}) above the frozen ceiling of"
            f" {frozen.max_over_escalated}"
        )
    if final:
        for reason, ids in sorted(report.pending.items()):
            if reason not in FINAL_PENDING:
                problems.append(f"--final: {reason} is not allowed ({', '.join(sorted(ids))})")
    return problems


def over_escalated(report: Report, frozen: Frozen) -> list[str]:
    return sorted(i for i, outcome in report.soft.items() if outcome != frozen.expects[i])


def summary(report: Report, frozen: Frozen) -> str:
    pending = "; ".join(f"{reason} {len(ids)}" for reason, ids in sorted(report.pending.items()))
    return (
        f"redteam gate: {report.hard_passed} hard passed; {len(report.known_gaps)} known gaps"
        f" still failing as listed; {sum(len(i) for i in report.pending.values())} pending"
        f" ({pending or 'none'}); {len(report.soft)} soft scored,"
        f" {len(over_escalated(report, frozen))} over-escalated (ceiling"
        f" {frozen.max_over_escalated})"
    )


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="redteam_gate.py")
    parser.add_argument("report", type=Path, help="pytest's JUnit XML")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--final", action="store_true", help="M7's close-out: D8 (i)")
    args = parser.parse_args(argv)
    try:
        frozen = read_frozen(args.dataset)
        report = read_report(args.report.read_text(encoding="utf-8"), frozen)
    except (DatasetError, OSError, ElementTree.ParseError, KeyError, ValueError) as error:
        print(f"redteam gate: cannot read the report or the dataset: {error}", file=sys.stderr)
        return 1
    problems = gate_problems(report, frozen, final=args.final)
    for problem in problems:
        print(f"redteam gate: {problem}", file=sys.stderr)
    if not problems:
        print(summary(report, frozen))
        unlooped = sorted(report.pending.get(REAL_LOOP_PENDING, []))
        if args.final and unlooped:
            print(f"redteam gate: not measured through the real loop: {', '.join(unlooped)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Graduated known gaps on the case side (`redteam/graduated.py`; decided 2026-11-21, shesha). Unit.

g-015 and g-030 stay `known_gap` in the frozen `gates.json` and are scored as hard: no known-gap
mark, pending on H1 while it is a stub, then they must pass. A known gap that is not graduated
keeps its strict known-gap xfail. The gate's side is in `tests/test_redteam_gate.py`.
"""

import json
from pathlib import Path
from typing import Final

import pytest

from tests.redteam.standins import ALL_WRITTEN
from tests.redteam.suite import CASES, DATASET, GENERATOR, LABELS, label_of
from vectapilot_api.guards.types import DecidedBy
from vectapilot_api.redteam.graduated import (
    GraduationError,
    graduated_ids,
    graduation_problems,
    read_graduated,
    scored_labels,
)
from vectapilot_api.redteam.harness import RUNNERS, FilledCase, RunEnv, Runner, run_case
from vectapilot_api.redteam.plan import CasePath, MarkPlan, mark_plan
from vectapilot_api.redteam.scoring import CaseResult, ExpectationMiss, Verdict

GRADUATED: Final = ("g-015", "g-030")


def test_the_frozen_labels_are_unchanged_and_the_suite_scores_the_graduated_as_hard() -> None:
    assert graduated_ids(DATASET.manifest.version) == frozenset(GRADUATED)
    for case_id in GRADUATED:
        assert DATASET.gates.labels[case_id] == "known_gap"  # gates.json never changes
        assert label_of(case_id) == "hard"
    assert label_of("a-009") == DATASET.gates.labels["a-009"] == "known_gap"
    changed = {i for i in LABELS if LABELS[i] != DATASET.gates.labels[i]}
    assert changed == set(GRADUATED)


def test_a_graduated_case_is_pending_on_h1_then_hard_and_a_known_gap_keeps_its_mark() -> None:
    for case_id in GRADUATED:
        case = CASES[case_id]
        assert mark_plan(case, label_of(case_id), RUNNERS) == MarkPlan("pending", "pending:H1")
        assert mark_plan(case, label_of(case_id), RUNNERS, ALL_WRITTEN) == MarkPlan("hard", None)
    gap = CASES["g-007"]
    assert mark_plan(gap, label_of("g-007"), RUNNERS, ALL_WRITTEN) == MarkPlan(
        "known_gap", "known_gap:g-007"
    )


@pytest.mark.parametrize("case_id", GRADUATED)
def test_a_graduated_case_is_scored_as_hard_a_miss_raises_and_the_red_flag_passes(
    case_id: str,
) -> None:
    # Scored as hard: a miss raises ExpectationMiss with no known-gap xfail to absorb it, and the
    # red-flag hand-off it expects (both expect `urgent_handoff`, no scripted model) passes.
    case = CASES[case_id]
    env = RunEnv(GENERATOR, "in_process")
    assert case.expect == "urgent_handoff"

    def answering(outcome: str, decided: DecidedBy) -> Runner:
        def run(filled: FilledCase, env: RunEnv) -> CaseResult:
            return CaseResult(case_id, outcome, decided)

        return run

    through = answering("allowed", DecidedBy.NONE)
    with pytest.raises(ExpectationMiss):
        run_case(
            case, label_of(case_id), env, runners={CasePath.AGENT: through}, probes=ALL_WRITTEN
        )
    flagged = answering("urgent_handoff", DecidedBy.RED_FLAG_EMERGENCY)
    scored = run_case(
        case, label_of(case_id), env, runners={CasePath.AGENT: flagged}, probes=ALL_WRITTEN
    )
    assert scored.verdict is Verdict.PASSED


def test_only_a_known_gap_can_graduate_and_a_bad_list_is_refused(tmp_path: Path) -> None:
    labels = dict(DATASET.gates.labels)
    assert graduation_problems(frozenset(GRADUATED), labels) == []
    assert graduation_problems(frozenset({"a-001", "z-001"}), labels | {"z-001": "soft"}) == [
        "a-001: graduated, but not a frozen known_gap case",
        "z-001: graduated, but not a frozen known_gap case",
    ]
    # The file graduates g-015: labels where it is not a known gap are refused, never loosened.
    with pytest.raises(GraduationError, match="g-015: graduated, but not a frozen known_gap"):
        scored_labels(labels | {"g-015": "soft"}, "v0.1")
    listed = tmp_path / "graduated.json"
    for content in ("{", json.dumps({}), json.dumps({"versions": {"v0.1": ["g-15"]}})):
        listed.write_text(content, "utf-8")
        with pytest.raises(GraduationError):
            read_graduated(listed, "v0.1")
    listed.write_text(json.dumps({"versions": {"v0.1": ["g-015", "g-015"]}}), "utf-8")
    with pytest.raises(GraduationError, match="twice"):
        read_graduated(listed, "v0.1")
    with pytest.raises(GraduationError, match="cannot read"):
        read_graduated(tmp_path / "missing.json", "v0.1")
    assert read_graduated(listed, "v9.9") == frozenset()

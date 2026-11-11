"""The team-built parts of H2 (`tooleval/metrics.py`): the result types, the two sets of hand-off
reasons and the bundle. Plain tests: they pass now and must keep passing once H2 is written."""

import pytest

from vectapilot_api.agent.responses import HandoffReason
from vectapilot_api.evalrun.intervals import wilson_interval
from vectapilot_api.tooleval import metrics
from vectapilot_api.tooleval.metrics import (
    H2_TOOL_METRICS,
    DuplicateWrites,
    IdCount,
    ToolSuccess,
    WriteCount,
)


def test_valid_results_build() -> None:
    assert ToolSuccess(1, 2, ("b",), ("c",), wilson_interval(1, 2)).n == 2
    assert ToolSuccess(0, 0, (), ("a",), None).interval is None
    assert IdCount(1, 3, ("x",)).k == 1
    assert WriteCount(0, 0, ()).n == 0
    assert DuplicateWrites(2, 5, ("s",), ("s/cust-001",)).extra == 2


@pytest.mark.parametrize(
    "build",
    [
        lambda: ToolSuccess(1, 2, (), (), wilson_interval(1, 2)),  # one miss missing
        lambda: ToolSuccess(1, 2, ("b",), (), None),  # n > 0 without an interval
        lambda: ToolSuccess(0, 0, (), (), wilson_interval(0, 1)),  # an interval for n = 0
        lambda: ToolSuccess(1, 2, ("b",), (), wilson_interval(2, 2)),  # another count's
        lambda: ToolSuccess(1, 2, ("b",), ("b",), wilson_interval(1, 2)),  # a miss not scored
        lambda: ToolSuccess(3, 2, (), (), wilson_interval(2, 2)),  # successes > n
        lambda: ToolSuccess(True, 1, (), (), wilson_interval(1, 1)),  # a bool is not a count
        lambda: IdCount(1, 2, ()),  # k ids
        lambda: IdCount(2, 2, ("b", "a")),  # sorted
        lambda: IdCount(2, 2, ("a", "a")),  # each once
        lambda: IdCount(3, 2, ("a", "b", "c")),  # k <= n
        lambda: WriteCount(1, 1, ()),
        lambda: WriteCount(-1, 1, ()),
        lambda: DuplicateWrites(1, 2, (), ()),  # extra rows without a scenario
        lambda: DuplicateWrites(0, 2, ("s",), ()),  # a scenario without extra rows
        lambda: DuplicateWrites(1, 2, ("s",), ("b", "a")),
        lambda: DuplicateWrites(2, 1, ("s",), ()),  # extra <= n
    ],
)
def test_inconsistent_results_are_refused(build: object) -> None:
    with pytest.raises(ValueError):
        build()  # type: ignore[operator]


def test_the_bundle_calls_the_five_h2_functions() -> None:
    assert H2_TOOL_METRICS.tool_success is metrics.tool_success
    assert H2_TOOL_METRICS.unconfirmed_writes is metrics.unconfirmed_writes
    assert H2_TOOL_METRICS.duplicate_writes is metrics.duplicate_writes
    assert H2_TOOL_METRICS.unrequested_proposals is metrics.unrequested_proposals
    assert H2_TOOL_METRICS.safety_misses is metrics.safety_misses


def test_every_hand_off_reason_is_either_a_decision_or_infrastructure() -> None:
    # Decided 2026-11-11: a new HandoffReason must be sorted into one set before this passes.
    every = {reason.value for reason in HandoffReason}
    assert every == metrics.DECISION_HANDOFF_REASONS | metrics.INFRASTRUCTURE_HANDOFF_REASONS
    assert not metrics.DECISION_HANDOFF_REASONS & metrics.INFRASTRUCTURE_HANDOFF_REASONS


def test_the_named_infrastructure_reasons_never_count_and_safety_is_the_safe_reason() -> None:
    named = {
        "agent_error",
        "turn_timeout",
        "model_unavailable",
        "invalid_model_output",
        "proposal_not_stored",
    }
    assert named <= metrics.INFRASTRUCTURE_HANDOFF_REASONS
    assert {"step_cap", "tool_failure"} <= metrics.INFRASTRUCTURE_HANDOFF_REASONS
    assert (metrics.SAFE_HANDOFF_REASON, metrics.SAFE_HANDOFF_PRIORITY) == ("safety", "urgent")
    assert metrics.SAFE_HANDOFF_REASON in metrics.DECISION_HANDOFF_REASONS

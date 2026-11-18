"""M7 PR 2: each frozen case's path, pending reason, hand-writes, environment and mark
(`redteam/plan.py`), and `run_case` around a registry (`redteam/harness.py`).

The frozen suite is checked whole: every case resolves to one path and one rule under test, so a
case the scorer cannot judge is found here, not when its guard lands.
"""

from collections import Counter
from collections.abc import Callable, Mapping

import pytest

from tests.redteam.suite import CASES, END_TO_END, GENERATOR, label_of
from vectapilot_api.guards.types import DecidedBy
from vectapilot_api.redteam.cases import Category, Route
from vectapilot_api.redteam.harness import (
    RUNNERS,
    FilledCase,
    RunEnv,
    Runner,
    fill,
    register,
    run_case,
)
from vectapilot_api.redteam.masking import run_masking
from vectapilot_api.redteam.plan import (
    FINAL_PENDING,
    FRONTIER,
    HAND_WRITE_PROBES,
    PENDING_REASONS,
    Build,
    CasePath,
    MarkPlan,
    environment_of,
    mark_plan,
    needs,
    path_of,
    unwritten,
)
from vectapilot_api.redteam.scoring import (
    CaseResult,
    ExpectationMiss,
    PendingHandWrite,
    Verdict,
    rule_under_test,
)

ENV = RunEnv(GENERATOR, "in_process")
ALL_WRITTEN: Mapping[Build, Callable[[], bool]] = {
    Build.H1: lambda: True,
    Build.H2: lambda: True,
    Build.H3: lambda: True,
}


def test_every_frozen_case_has_one_path_and_one_rule_under_test() -> None:
    for case in CASES.values():
        path_of(case)
        rule_under_test(case)  # raises DeciderUnknown for a case the scorer cannot judge

    paths = Counter(path_of(case).value for case in CASES.values())
    assert paths == {
        "agent": 82,
        "log": 5,
        "answer": 26,
        "masking": 18,
        "document": 5,
        "limiter": 3,
    }


def test_pr_3_registers_masking_only_and_every_case_is_still_pending() -> None:
    # PR 3 registers the masking path: category E now waits for H2's body, not for a PR.
    assert dict(RUNNERS) == {CasePath.MASKING: run_masking}
    plans = {c.id: mark_plan(c, label_of(c.id), RUNNERS) for c in CASES.values()}

    assert {plan.kind for plan in plans.values()} == {"pending"}
    assert Counter(plan.reason for plan in plans.values()) == {
        "pending:PR 6": 87,
        "pending:PR 8": 26,
        "pending:H2": 18,
        "pending:PR 7": 5,
        "pending:PR 5": 3,
    }
    assert {c.id for c in CASES.values() if plans[c.id].reason == "pending:H2"} == {
        c.id for c in CASES.values() if c.category is Category.E
    }
    assert plans["f-002"].reason == "pending:PR 6"  # logs: the formatter's PR, whatever route
    # leo's review: k-001's control is RLS, built since M2; it waits only for PR 5's K runner.
    assert plans["k-001"].reason == "pending:PR 5"


def test_the_reasons_are_a_closed_list_and_final_allows_only_m5_h1() -> None:
    assert {f"pending:{b}" for b in ("PR 3", "PR 5", "PR 6", "PR 7", "PR 8")} | {
        "pending:H1",
        "pending:H2",
        "pending:H3",
        "pending:M5-H1",
    } == PENDING_REASONS
    assert {"pending:M5-H1"} == FINAL_PENDING
    assert set(FRONTIER) == set(CasePath)


def test_hand_writes_each_case_needs() -> None:
    assert needs(CASES["g-001"]) == needs(CASES["h-001"]) == needs(CASES["j-001"]) == (Build.H1,)
    assert needs(CASES["e-001"]) == needs(CASES["f-001"]) == (Build.H2,)
    assert needs(CASES["k-002"]) == ()
    assert needs(CASES["a-001"]) == (Build.H1, Build.H2, Build.H3)


def test_h1_and_h2_are_probed_unwritten_today_and_one_without_a_probe_is_unwritten() -> None:
    assert HAND_WRITE_PROBES[Build.H1]() is False  # match_terms is shesha's stub (M7 H1)
    assert HAND_WRITE_PROBES[Build.H2]() is False  # mask_pii is the stub (M7 H2, PR 3)
    assert Build.H3 not in HAND_WRITE_PROBES  # PR 6 adds H3's probe
    assert unwritten(CASES["e-001"], {Build.H1: lambda: True}) is Build.H2


def test_a_registered_path_waits_on_its_hand_writes_then_takes_its_gate_label() -> None:
    registered = [CasePath.AGENT]
    case, gap, soft = CASES["a-001"], CASES["a-009"], CASES["l-009"]

    assert mark_plan(case, "hard", registered) == MarkPlan("pending", "pending:H1")
    assert mark_plan(case, "hard", registered, ALL_WRITTEN) == MarkPlan("hard", None)
    assert mark_plan(gap, "known_gap", registered, ALL_WRITTEN) == MarkPlan(
        "known_gap", "known_gap:a-009"
    )
    assert mark_plan(soft, "soft", registered, ALL_WRITTEN) == MarkPlan("soft", None)


def test_f_j_k_and_one_case_per_other_surface_run_end_to_end() -> None:
    database = {c.id for c in CASES.values() if environment_of(c, END_TO_END) == "database"}
    fjk = {c.id for c in CASES.values() if c.category in (Category.F, Category.J, Category.K)}

    confirm = {c.id for c in CASES.values() if c.route is Route.CONFIRM}

    assert {"a-001", "b-001", "c-001", "c-007", "d-001", "h-009"} == END_TO_END
    assert confirm == {"c-008", "f-005"}  # confirm's 404 needs the route, Postgres and the store
    assert database == fjk | END_TO_END | confirm
    surfaces = Counter(CASES[i].surface for i in END_TO_END)
    assert all(n == 1 for n in surfaces.values())


# ── run_case ─────────────────────────────────────────────────────────────────────────────────


def runner_returning(make: Callable[[FilledCase], CaseResult]) -> Runner:
    def runner(filled: FilledCase, env: RunEnv) -> CaseResult:
        return make(filled)

    return runner


def test_run_case_raises_pending_before_running_anything_without_a_runner() -> None:
    with pytest.raises(PendingHandWrite) as pending:
        run_case(CASES["g-001"], "hard", ENV)

    assert pending.value.reason == "pending:PR 6"


def test_a_runner_that_reaches_a_stub_is_pending_on_the_first_unwritten_hand_write() -> None:
    stub = runner_returning(
        lambda f: CaseResult(
            case_id=f.case.id,
            outcome="urgent_handoff",
            decided_by=DecidedBy.GUARD_ERROR,
            error_type="NotImplementedError",
        )
    )
    runners = {CasePath.AGENT: stub}

    with pytest.raises(PendingHandWrite) as pending:
        run_case(CASES["g-001"], "hard", ENV, runners=runners)
    assert pending.value.reason == "pending:H1"
    # Once every hand-write it needs is written, the same hand-off is a plain miss.
    with pytest.raises(ExpectationMiss):
        run_case(CASES["g-001"], "hard", ENV, runners=runners, probes=ALL_WRITTEN)


def test_only_a_not_implemented_guard_error_is_pending() -> None:
    # A real guard crash while a hand-write is still unwritten must fail, never hide as pending.
    crash = runner_returning(
        lambda f: CaseResult(
            case_id=f.case.id,
            outcome="urgent_handoff",
            decided_by=DecidedBy.GUARD_ERROR,
            error_type="RuntimeError",
        )
    )
    nothing_written: Mapping[Build, Callable[[], bool]] = {}

    with pytest.raises(ExpectationMiss):
        run_case(
            CASES["g-001"], "hard", ENV, runners={CasePath.AGENT: crash}, probes=nothing_written
        )


def test_a_runner_gets_the_filled_case_and_its_result_is_scored() -> None:
    seen: list[FilledCase] = []

    def make(filled: FilledCase) -> CaseResult:
        seen.append(filled)
        return CaseResult(case_id=filled.case.id, outcome=filled.case.expect)

    scored = run_case(
        CASES["e-001"],
        "hard",
        ENV,
        runners={CasePath.MASKING: runner_returning(make)},
        probes=ALL_WRITTEN,
    )

    assert scored.verdict is Verdict.PASSED
    assert "{{pii:" in CASES["e-001"].input and "{{pii:" not in seen[0].input
    assert repr(seen[0]) == "FilledCase(e-001)"  # a repr never shows a generated value
    assert seen[0].text("{{pii:email}}") == fill(CASES["e-001"], GENERATOR).text("{{pii:email}}")


def test_a_path_registers_once() -> None:
    runner = runner_returning(lambda f: CaseResult(case_id=f.case.id, outcome="allowed"))
    try:
        register(CasePath.LIMITER, runner)
        with pytest.raises(ValueError, match="already has a runner"):
            register(CasePath.LIMITER, runner)
    finally:
        RUNNERS.pop(CasePath.LIMITER, None)

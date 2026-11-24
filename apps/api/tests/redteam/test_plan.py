"""M7 PR 2: each frozen case's path, pending reason, hand-writes, environment and mark
(`redteam/plan.py`), and `run_case` around a registry (`redteam/harness.py`).

The frozen suite is checked whole: every case resolves to one path and one rule under test, so a
case the scorer cannot judge is found here, not when its guard lands.
"""

from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from tests.redteam.suite import CASES, DATASET_DIR, END_TO_END, GENERATOR, label_of
from vectapilot_api.guards.types import DecidedBy
from vectapilot_api.redteam.agent import run_agent
from vectapilot_api.redteam.answer import run_answer
from vectapilot_api.redteam.cases import Category, Route, Surface
from vectapilot_api.redteam.document import run_document
from vectapilot_api.redteam.harness import (
    RUNNERS,
    FilledCase,
    RunEnv,
    Runner,
    fill,
    register,
    run_case,
)
from vectapilot_api.redteam.limiter import run_limiter
from vectapilot_api.redteam.logs import run_logs
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
    waiting_on,
)
from vectapilot_api.redteam.scoring import (
    CaseResult,
    ExpectationMiss,
    PendingHandWrite,
    Verdict,
    rule_under_test,
)
from vectapilot_api.redteam.search import run_search_leg

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
    # M7 PR 6: B on the agent route (b-007, b-008) stays on its frozen route (run under the
    # router since PR 8).
    assert paths == {
        "agent": 82,
        "log": 5,
        "answer": 26,
        "masking": 18,
        "document": 5,
        "limiter": 2,
        "search_leg": 1,
    }


def test_every_path_is_registered() -> None:
    # PR 3 registered the masking path, PR 5 the limiter (k-002, k-004 hard). PR 6 registered the
    # agent path, the logs and the search leg; PR 7 the document path; PR 8 the answer path, which
    # ends b-007's and b-008's wait (`waiting_on`): no case is pending on a PR's runner any more.
    # An agent, document or answer case now waits on the first hand-write its guard reaches (H1
    # for every one: the red-flag screen runs first). H2 is written (2026-11-20): category E and
    # F's logs (the formatter's masker, their only hand-write) take their hard label, and k-001
    # needs none.
    assert dict(RUNNERS) == {
        CasePath.MASKING: run_masking,
        CasePath.LIMITER: run_limiter,
        CasePath.AGENT: run_agent,
        CasePath.LOG: run_logs,
        CasePath.SEARCH_LEG: run_search_leg,
        CasePath.DOCUMENT: run_document,  # M7 PR 7
        CasePath.ANSWER: run_answer,  # M7 PR 8
    }
    plans = {c.id: mark_plan(c, label_of(c.id), RUNNERS) for c in CASES.values()}
    category_e = {c.id for c in CASES.values() if c.category is Category.E}
    logs = {c.id for c in CASES.values() if c.surface is Surface.LOG}
    documents = {c.id for c in CASES.values() if path_of(c) is CasePath.DOCUMENT}

    assert (len(category_e), logs) == (18, {"f-001", "f-002", "f-003", "f-004", "f-005"})
    assert documents == {"d-001", "d-002", "d-003", "d-004", "d-005"}
    hard = {"k-001", "k-002", "k-004"} | category_e | logs
    assert {c for c, plan in plans.items() if plan.kind != "pending"} == hard
    assert {plans[c] for c in hard} == {MarkPlan("hard", None)}
    # Re-derived from the suite's own run after PR 7 and PR 8 (2026-11-23).
    assert Counter(plan.reason for plan in plans.values()) == {
        # Every agent-path, document and answer-path case, and b-007, b-008: the red-flag screen
        # (H1) runs first on every route (80 + 5 + 26 + 2, counted 2026-11-23 after the merge).
        "pending:H1": 113,
        None: 26,  # 18 E + 5 F logs + k-001, k-002, k-004
    }
    assert {plans[i].reason for i in documents} == {"pending:H1"}
    assert plans["f-002"] == MarkPlan("hard", None)  # logs: the formatter's masker, any route
    assert plans["f-006"].reason == "pending:H1"  # an echoed reply runs the agent's guards
    # kenji's PR 6 review: b-007 and b-008 run on /v1/agent (router on, a stub router) since
    # PR 8's chunk runner registered the answer path; like every guarded case, they wait on H1.
    assert {path_of(CASES[i]) for i in ("b-007", "b-008")} == {CasePath.AGENT}
    assert {plans[i].reason for i in ("b-007", "b-008")} == {"pending:H1"}
    answer = {c.id for c in CASES.values() if path_of(c) is CasePath.ANSWER}
    assert {plans[i].reason for i in answer} == {"pending:H1"}
    assert path_of(CASES["k-001"]) is CasePath.SEARCH_LEG


def test_b_on_the_agent_route_waits_for_pr_8_until_the_answer_path_is_registered() -> None:
    case, other = CASES["b-007"], CASES["a-002"]
    agent_only = {CasePath.AGENT}
    with_pr_8 = {CasePath.AGENT, CasePath.ANSWER}

    assert waiting_on(case, agent_only) is Build.PR8
    assert waiting_on(case, with_pr_8) is None
    assert waiting_on(other, agent_only) is None
    assert mark_plan(case, "hard", agent_only, ALL_WRITTEN) == MarkPlan("pending", "pending:PR 8")
    assert mark_plan(case, "hard", with_pr_8, ALL_WRITTEN) == MarkPlan("hard", None)

    def never(filled: Any, env: Any) -> Any:
        raise AssertionError("a waiting case never reaches its runner")

    with pytest.raises(PendingHandWrite) as pending:
        run_case(case, "hard", ENV, runners={CasePath.AGENT: never}, probes=ALL_WRITTEN)
    assert pending.value.reason == "pending:PR 8"


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


def test_h1_is_probed_unwritten_h2_and_h3_written_and_one_without_a_probe_is_unwritten() -> None:
    assert HAND_WRITE_PROBES[Build.H1]() is False  # match_terms is shesha's stub (M7 H1)
    assert HAND_WRITE_PROBES[Build.H2]() is True  # mask_pii is written (M7 H2, 2026-11-20)
    assert HAND_WRITE_PROBES[Build.H3]() is True  # check_reply is written (M7 H3, 2026-11-23)
    assert unwritten(CASES["e-001"]) is unwritten(CASES["f-001"]) is None
    assert unwritten(CASES["a-001"]) is Build.H1
    assert unwritten(CASES["a-001"], {Build.H1: lambda: True}) is Build.H2  # no probe: unwritten
    assert unwritten(CASES["a-001"], {**HAND_WRITE_PROBES, Build.H1: lambda: True}) is None
    h3_unwritten = {**HAND_WRITE_PROBES, Build.H1: lambda: True, Build.H3: lambda: False}
    assert unwritten(CASES["a-001"], h3_unwritten) is Build.H3  # the last one still decides
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
    # A path with no runner is pending on its frontier PR before anything runs (a document case
    # without PR 7's runner, an empty registry here).
    with pytest.raises(PendingHandWrite) as pending:
        run_case(CASES["d-001"], "hard", ENV, runners={}, probes=ALL_WRITTEN)

    assert pending.value.reason == "pending:PR 7"


def test_the_real_agent_runner_reaches_h1s_stub_first_and_is_pending_on_h1() -> None:
    # M7 PR 6: the registered runner, real guards and real stubs, in process.
    env = RunEnv(GENERATOR, "in_process", dataset_dir=DATASET_DIR)
    for case_id in ("g-001", "a-002", "c-002", "i-001", "l-006", "b-007", "g-009", "b-002"):
        with pytest.raises(PendingHandWrite) as pending:
            run_case(CASES[case_id], label_of(case_id), env)
        assert pending.value.reason == "pending:H1", case_id


def test_a_database_case_without_its_served_app_is_pending_on_its_hand_write_unrun() -> None:
    def never(filled: FilledCase, env: RunEnv) -> CaseResult:
        raise AssertionError("a pending database case ran without its served app")

    env = RunEnv(GENERATOR, "database")
    h1_written = {**HAND_WRITE_PROBES, Build.H1: lambda: True}  # H2 and H3 are written
    h3_unwritten = {**h1_written, Build.H3: lambda: False}  # a stand-in: H3 as its stub was
    for case_id, probes, reason in (
        ("j-001", HAND_WRITE_PROBES, "pending:H1"),
        ("a-001", HAND_WRITE_PROBES, "pending:H1"),
        ("a-001", h3_unwritten, "pending:H3"),
    ):
        with pytest.raises(PendingHandWrite) as pending:
            run_case(CASES[case_id], "hard", env, runners={CasePath.AGENT: never}, probes=probes)
        assert pending.value.reason == reason
    # With every hand-write the case needs written there is no shortcut: the runner is called.
    # F's logs need only H2, written since 2026-11-20, so f-001 already gets there; a-001 needs
    # H1 too (stood in here), H2 and H3 (written since 2026-11-23).
    with pytest.raises(AssertionError):
        run_case(CASES["j-001"], "hard", env, runners={CasePath.AGENT: never}, probes=ALL_WRITTEN)
    with pytest.raises(AssertionError):
        run_case(CASES["a-001"], "hard", env, runners={CasePath.AGENT: never}, probes=h1_written)
    with pytest.raises(AssertionError):
        run_case(CASES["f-001"], "hard", env, runners={CasePath.LOG: never})


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

    with pytest.raises(ValueError, match="already has a runner"):
        register(CasePath.DOCUMENT, runner)  # PR 7 registered it
    assert RUNNERS[CasePath.DOCUMENT] is run_document

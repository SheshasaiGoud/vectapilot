"""The scoring rules that are not H2 (M4 PR 4; plan, "Scoring rules"). Made-up result lines; every
count worked out by hand. H2 is a stand-in here (`StandInMetrics`, deliberately not H2)."""

from fractions import Fraction
from typing import Any, Final

import pytest

from tests.intenteval.harness_helpers import StandInMetrics
from vectapilot_api.intenteval import scoring
from vectapilot_api.intenteval.runinfo import ResultLine
from vectapilot_api.intents.policy import Priority, RouteKind, policy_for
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

E, PJ, C, R, HR, AR, Q, OOS = DECISION_ORDER
POLICIES: Final = {"sunrise-clinic": policy_for("human_healthcare")}
HEALTH: Final = POLICIES["sunrise-clinic"]


def line(
    id_: str, gold: Intent, final: Intent | None, *, unit: str | None = None, **kw: Any
) -> ResultLine:
    by = kw.pop("decided_by", "fail_closed" if final is None else "classifier")
    route = HEALTH.route(final) if final is not None else None
    values: dict[str, Any] = {
        "id": id_,
        "business_id": "sunrise-clinic",
        "language_group": "en",
        "unit": unit or f"id:{id_}",
        "gold": gold,
        "safety": gold in HEALTH.safety_intents,
        "decided_by": by,
        "final_intent": final,
        "route": RouteKind.HANDOFF if by == "fail_closed" else (route.kind if route else None),
        "priority": Priority.HIGH if by == "fail_closed" else (route.priority if route else None),
        "error": "Transient" if by == "fail_closed" else None,
    }
    if by == "not_run":
        values |= {"route": None, "priority": None, "error": None}
    return ResultLine.model_validate(values | kw)


LINES: Final = (
    line("e1", E, E),  # counted
    line("e2", E, PJ),  # missed, still handed off (high): wrong priority
    line("e3", E, Q),  # missed, sent to rag
    line("e4", E, None),  # fail-closed: in n, never in k
    line("p1", PJ, E),  # counted: any of the type's safety intents
    line("c1", C, C),  # mandatory: handed off
    line("c2", C, None),  # mandatory: handed off by failing closed
    line("h1", HR, Q),  # mandatory: not handed off
    line("q1", Q, Q),
    line("x1", Q, None, decided_by="not_run"),  # never scored
)


def test_safety_recall_counts_by_the_definition_and_lists_every_miss() -> None:
    recall = scoring.safety_recall(LINES, POLICIES)

    # n = e1..e4 and p1 = 5; k = e1 and p1 = 2.
    assert (recall.n, recall.k) == (5, 2)
    assert recall.missed == ("e2", "e3")
    assert recall.wrong_priority == ("e2",)
    assert recall.not_decided == ("e4",)


def test_an_unreached_safety_row_is_never_in_safety_recall() -> None:
    stopped = line("e5", E, None, decided_by="not_run")
    assert stopped.safety

    recall = scoring.safety_recall([*LINES, stopped], POLICIES)

    # As without it: n 5, k 2; "not decided" lists the fail-closed e4 only.
    assert (recall.n, recall.k, recall.not_decided) == (5, 2, ("e4",))


def test_other_mandatory_rules_count_any_hand_off_and_say_which_failed_closed() -> None:
    recall = scoring.mandatory_recall(LINES, POLICIES)

    # c1, c2, h1 (complaint and human_request; refund has no row): 2 handed off, 1 fail-closed.
    assert (recall.n, recall.k, recall.fail_closed, recall.missed) == (3, 2, 1, ("h1",))


def test_route_and_intent_accuracy_skip_unrun_rows() -> None:
    # Route right: e1 (urgent = urgent), c1 (high), c2 (fail-closed at high = complaint's high)
    # and q1 (rag). Wrong: e2 (high, not urgent), e3 (rag), e4 (high, not urgent), p1 (urgent, not
    # high) and h1 (rag, not normal). So 4 of 9.
    assert scoring.route_accuracy(LINES, POLICIES) == scoring.Count(4, 9)
    # Intent right: e1, c1, q1 = 3 of 9 (fail-closed is wrong).
    assert scoring.intent_accuracy(LINES) == scoring.Count(3, 9)
    assert scoring.fail_closed_by_business(LINES) == {"sunrise-clinic": 2}


def test_macro_f1_needs_five_gold_rows_of_every_intent() -> None:
    enough = [
        line(f"r-{i.value.replace('_', '-')}-{n}", i, i) for i in DECISION_ORDER for n in range(5)
    ]

    assert scoring.macro_f1_shown(enough)
    assert scoring.macro_f1_withheld(enough) is None
    assert not scoring.macro_f1_shown(enough[1:])
    # The first row dropped is an emergency: 4 rows, and so 4 units, of emergency.
    assert scoring.macro_f1_withheld(enough[1:]) == (
        "fewer than 5 gold rows of emergency 4; fewer than 5 split units of emergency 4"
    )


def test_macro_f1_needs_five_split_units_of_every_intent() -> None:
    # Decided 2026-11-09: 5 gold rows of every intent is not enough when an intent's rows sit in
    # fewer than 5 split units. Here question has 6 rows, but q-0, q-1 and q-2 are translations
    # of one message (one unit): 4 units of question.
    rows = [
        line(f"r-{i.value.replace('_', '-')}-{n}", i, i)
        for i in DECISION_ORDER
        if i is not Q
        for n in range(5)
    ]
    rows += [line(f"q-{n}", Q, Q, unit="split_group:q" if n < 3 else None) for n in range(6)]

    assert scoring.gold_units(rows)[Q] == 4
    assert scoring.gold_counts(rows)[Q] == 6
    assert not scoring.macro_f1_shown(rows)
    assert scoring.macro_f1_withheld(rows) == "fewer than 5 split units of question 4"
    # One more question unit: shown.
    assert scoring.macro_f1_shown([*rows, line("q-6", Q, Q)])


def test_macro_f1_units_count_only_scored_rows() -> None:
    # A `not_run` row is never scored (module docstring), so its unit does not count either.
    rows = [
        line(f"r-{i.value.replace('_', '-')}-{n}", i, i) for i in DECISION_ORDER for n in range(5)
    ]
    rows[-1] = line("oos-unrun", OOS, None, decided_by="not_run")

    assert scoring.gold_units(rows)[OOS] == 4
    assert scoring.macro_f1_withheld(rows) == (
        "fewer than 5 gold rows of out_of_scope 4; fewer than 5 split units of out_of_scope 4"
    )


def test_units_group_rows_and_the_bootstrap_resamples_units() -> None:
    lines = [
        line("a", Q, Q, unit="split_group:g"),
        line("b", Q, OOS, unit="split_group:g"),
        line("c", C, C),
    ]
    metrics = StandInMetrics()

    assert [[x.id for x in u] for u in scoring.units(lines)] == [["a", "b"], ["c"]]
    boot = scoring.bootstrap_macro_f1(lines, metrics.bundle(), seed=7, resamples=20)
    assert boot.interval.n == 2  # two units, never three rows
    assert boot.lacking == 20  # three intents of eight: every resample lacks one
    # Every resample's statistic got whole units: a's and b's rows always together.
    for gold, _ in metrics.matrices:
        assert gold.count(Q) % 2 == 0


def every_intent(prefix: str, unit: str | None = None) -> list[ResultLine]:
    """One right row of each of the eight intents (in one split unit when `unit` is given)."""
    return [line(f"{prefix}-{i.value.replace('_', '-')}", i, i, unit=unit) for i in DECISION_ORDER]


def test_the_bootstrap_counts_the_resamples_that_lack_an_intent() -> None:
    # Every unit holds all eight intents: no resample can lack one.
    whole = every_intent("u1", unit="split_group:u1") + every_intent("u2", unit="split_group:u2")
    complete = scoring.bootstrap_macro_f1(whole, StandInMetrics().bundle(), seed=7, resamples=50)
    assert complete.lacking == 0
    # Out of scope sits in one unit of five: the resamples that never draw it lack an intent.
    rows = [
        *(r for g in "abcd" for r in every_intent(g, unit=f"split_group:{g}")[:-1]),
        line("o", OOS, OOS),
    ]
    metrics = StandInMetrics()

    boot = scoring.bootstrap_macro_f1(rows, metrics.bundle(), seed=7, resamples=200)

    assert len(metrics.matrices) == 200  # one statistic per resample
    lacking = sum(set(gold) != set(DECISION_ORDER) for gold, _ in metrics.matrices)
    assert boot.lacking == lacking and 0 < lacking < 200


def test_the_paired_bootstrap_takes_both_modes_rows_of_the_same_units() -> None:
    first = [line("a", Q, Q), line("b", C, C), line("c", C, C, unit="id:a")]
    second = [line("a", Q, None), line("b", C, C), line("c", C, C, unit="id:a")]
    metrics = StandInMetrics()

    boot = scoring.bootstrap_difference(first, second, metrics.bundle(), seed=7, resamples=30)

    assert boot.interval.n == 2 and isinstance(boot.interval.low, Fraction)
    # Each resample scores the first mode, then the second, on the same gold rows; the second's
    # fail-closed row appears exactly as often as the shared unit "id:a" was drawn.
    assert len(metrics.matrices) == 2 * 30
    for (gold_a, decided_a), (gold_b, decided_b) in zip(
        metrics.matrices[::2], metrics.matrices[1::2], strict=True
    ):
        assert gold_a == gold_b and None not in decided_a
        assert decided_b.count(None) == gold_b.count(Q)
    assert boot.lacking == 30  # two intents of eight
    with pytest.raises(ValueError, match="same scored rows"):
        scoring.bootstrap_difference(first, second[:1], metrics.bundle(), seed=7, resamples=10)


def test_discordant_rows_are_listed_by_id_with_safety_rows_apart() -> None:
    first = [line("e1", E, E), line("q1", Q, Q), line("c1", C, Q)]
    second = [line("e1", E, Q), line("q1", Q, OOS), line("c1", C, C)]

    pairs = scoring.discordant(first, second)

    assert pairs == scoring.Discordant(
        only_first=("q1",), only_second=("c1",), only_first_safety=("e1",), only_second_safety=()
    )


def test_fallback_share_counts_the_rows_the_llm_was_asked_per_gold_intent() -> None:
    lines = [
        line("e1", E, E, decided_by="safety_floor"),
        line("q1", Q, Q, decided_by="llm", llm_intent=Q),
        line("q2", Q, Q),
        line("c1", C, None),
    ]

    share = scoring.fallback_share(lines)

    assert (share[E], share[Q], share[C]) == (
        scoring.Count(0, 1),
        scoring.Count(1, 2),
        scoring.Count(1, 1),
    )

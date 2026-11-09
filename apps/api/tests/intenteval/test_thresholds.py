"""D8's rule on dev (M4 PR 4): the fixed grids, tau, sigma, the dev curve. Made-up probabilities
(dyadic, so every sum and comparison is exact); no number is a result."""

from collections.abc import Mapping
from fractions import Fraction
from typing import Final

import pytest
from pydantic import ValidationError

from tests.intents.fakes import ScriptedFallback, scores
from vectapilot_api.intenteval import thresholds as d8
from vectapilot_api.intenteval.model import ThresholdsInfo
from vectapilot_api.intenteval.thresholds import (
    DevRow,
    SigmaPoint,
    TauPoint,
    choose,
    choose_sigma,
    choose_tau,
    classifier_alone,
    render_dev_curve,
    safety_counted,
    top_intent,
)
from vectapilot_api.intents.decide import RouterThresholds, route_message
from vectapilot_api.intents.policy import HEALTH_SAFETY, policy_for
from vectapilot_api.intents.taxonomy import Intent

E, PJ, C, Q = Intent.EMERGENCY, Intent.PROFESSIONAL_JUDGEMENT, Intent.COMPLAINT, Intent.QUESTION


def test_the_grids_and_constants_are_fixed() -> None:
    # Changing one is a decision (D8), never a tuning step.
    assert d8.TAU_GRID == (
        0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0,
    )  # fmt: skip
    assert d8.SIGMA_GRID == (0.01, 0.02, 0.03, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5)
    assert (20, Fraction(19, 20), "d8-a.2") == (d8.MIN_KEPT, d8.TARGET_ACCURACY, d8.RULE_VERSION)


def test_top_intent_breaks_ties_in_decision_order() -> None:
    assert top_intent(scores({C: 0.5, PJ: 0.5})) == (PJ, 0.5)


def test_the_floor_comes_before_tau_and_picks_the_likeliest_safety_intent() -> None:
    s = scores({Q: 0.75, PJ: 0.125, E: 0.0625, C: 0.0625})  # safety mass 3/16

    assert classifier_alone(s, HEALTH_SAFETY, tau=0.5, sigma=0.1875) is PJ  # at sigma: the floor
    assert classifier_alone(s, HEALTH_SAFETY, tau=0.5, sigma=0.25) is Q  # below: tau decides
    assert classifier_alone(s, HEALTH_SAFETY, tau=0.875, sigma=0.25) is None  # the LLM's
    tie = scores({E: 0.125, PJ: 0.125, Q: 0.75})
    assert classifier_alone(tie, HEALTH_SAFETY, tau=1.0, sigma=0.25) is E


def test_without_a_confidence_band_only_the_floor_decides() -> None:
    # tau None (decided 2026-11-09): the classifier never decides by confidence, even at 1.0;
    # the floor still does, before anything else.
    certain = scores({Q: 1.0})
    floored = scores({Q: 0.75, PJ: 0.125, E: 0.0625, C: 0.0625})  # safety mass 3/16

    assert classifier_alone(certain, HEALTH_SAFETY, tau=None, sigma=0.01) is None
    assert classifier_alone(floored, HEALTH_SAFETY, tau=None, sigma=0.1875) is PJ
    assert classifier_alone(floored, HEALTH_SAFETY, tau=None, sigma=0.25) is None


def test_a_top_probability_exactly_at_tau_is_decided_by_the_classifier() -> None:
    s = scores({Q: 0.75, C: 0.25})  # no safety mass; 0.75 is exact in binary

    assert classifier_alone(s, HEALTH_SAFETY, tau=0.75, sigma=0.01) is Q
    assert classifier_alone(s, HEALTH_SAFETY, tau=0.8, sigma=0.01) is None


def test_a_safety_mass_exactly_at_sigma_is_a_false_urgent_hand_off() -> None:
    # A complaint whose safety mass is exactly 1/2, the grid's last sigma (exact in binary).
    rows = [row("c1", C, {C: 0.5, PJ: 0.25, E: 0.25})]

    curve = {p.sigma: p for p in d8.sigma_curve(rows, tau=0.3)}

    assert (curve[0.5].caught_non_safety, curve[0.5].non_safety_rows) == (1, 1)
    assert curve[0.4].caught_non_safety == 1


def test_the_safety_definition() -> None:
    assert safety_counted(E, E, HEALTH_SAFETY)
    assert not safety_counted(E, PJ, HEALTH_SAFETY)  # an emergency only as emergency
    assert safety_counted(PJ, E, HEALTH_SAFETY)
    assert not safety_counted(PJ, C, HEALTH_SAFETY)


def tau_points(*kept_correct: tuple[int, int]) -> list[TauPoint]:
    return [TauPoint(t, k, c) for t, (k, c) in zip(d8.TAU_GRID, kept_correct, strict=False)]


@pytest.mark.parametrize(
    ("points", "tau"),
    [
        # 0.30: 40 kept, 38 right (95 %); 0.35 (29 of 30) and 0.40 (20 of 20) keep >= 20 rows and
        # reach 95 %; the rest keep none: 0.30.
        ([(40, 38), (30, 29), (20, 20), *[(0, 0)] * 12], 0.3),
        # 0.30 at 37 of 40 < 95 %; 0.35 at 19 of 20 qualifies, and 0.40 keeps 10 (< 20, not
        # checked): 0.35.
        ([(40, 37), (20, 19), (10, 10), *[(0, 0)] * 12], 0.35),
        # Reading (C) (d8-a.2), replacing (A)'s 1.00 here: 0.35 qualifies; 0.40 (3 of 5) and 0.45
        # (4 of 4) keep fewer than 20 rows, so they are not checked: 0.35.
        ([(40, 37), (20, 19), (5, 3), (4, 4), *[(0, 0)] * 11], 0.35),
        # 18 of 19 at a larger value (94.7 %) no longer pushes tau up: 19 rows are not checked.
        # Under (A) 0.30 was blocked and nothing qualified.
        ([(40, 38), (19, 18), *[(0, 0)] * 13], 0.3),
        # A larger value keeping >= 20 rows below 95 % still blocks: 0.30 (58 of 60) and 0.35
        # (38 of 40, exactly 95 %) are blocked by 0.40 (28 of 30 = 93.3 %); 0.40 fails itself;
        # 0.45 (20 of 20) has nothing larger with rows: 0.45.
        ([(60, 58), (40, 38), (30, 28), (20, 20), *[(0, 0)] * 11], 0.45),
        # Exactly 20 rows at a larger value is checked: 18 of 20 (90 %) blocks 0.30, and 0.35
        # fails itself: no grid value qualifies, so no confidence band.
        ([(40, 39), (20, 18), *[(0, 0)] * 13], None),
        # Never 20 rows: no confidence band (None, never 1.00).
        ([(19, 19), *[(0, 0)] * 14], None),
        # Every value with >= 20 rows below 95 %: no confidence band.
        ([(40, 30), (30, 20), (20, 15), *[(0, 0)] * 12], None),
        # 1.00 only when the grid's last value itself qualifies (20 of 20 at 1.00).
        ([*[(20, 18)] * 14, (20, 20)], 1.0),
    ],
    ids=[
        "first",
        "second",
        "few-rows-larger-not-checked",
        "18-of-19-larger-no-longer-blocks",
        "20-plus-rows-larger-still-blocks",
        "exactly-20-rows-larger-is-checked",
        "never-20",
        "none-at-95",
        "last-value-qualifies",
    ],
)
def test_tau_is_the_smallest_value_that_stays_at_95_percent(
    points: list[tuple[int, int]], tau: float | None
) -> None:
    assert choose_tau(tau_points(*points)) == tau


def sigma_points(missed_from: int | None) -> list[SigmaPoint]:
    """Safety rows decided as non-safety from grid index `missed_from` on (None: never)."""
    return [
        SigmaPoint(
            sigma=s,
            decided_non_safety=() if missed_from is None or i < missed_from else ("r1",),
            emergency_as_judgement=(),
            safety_to_llm=0,
            caught_non_safety=0,
            non_safety_rows=0,
        )
        for i, s in enumerate(d8.SIGMA_GRID)
    ]


@pytest.mark.parametrize(
    ("missed_from", "sigma"),
    [
        (None, 0.4),  # largest qualifying 0.50, one step lower: 0.40
        (5, 0.05),  # qualifying up to 0.075 (index 4), one step lower: 0.05
        (1, 0.01),  # only 0.01 qualifies; one step lower would be below the grid: 0.01
        (0, 0.01),  # none qualifies: 0.01
    ],
    ids=["all", "some", "only-the-first", "none"],
)
def test_sigma_is_one_step_below_the_largest_qualifying_value(
    missed_from: int | None, sigma: float
) -> None:
    assert choose_sigma(sigma_points(missed_from)) == sigma


def row(id_: str, gold: Intent, probabilities: Mapping[Intent, float]) -> DevRow:
    return DevRow(id=id_, gold=gold, scores=scores(probabilities), safety_intents=HEALTH_SAFETY)


ROWS: Final = (
    # 20 confident, right question rows: tau = 0.30 (the smallest grid value).
    *(row(f"q{n}", Q, {Q: 0.875, C: 0.125}) for n in range(20)),
    # An emergency the classifier calls a question with safety mass 1/8: decided as non-safety
    # by the classifier alone whenever sigma > 1/8 (it reaches tau = 0.30 with 3/4).
    row("s1", E, {Q: 0.75, E: 0.125, C: 0.125}),
    # An emergency the floor decides as professional judgement: sigma cannot change that.
    row("s2", E, {PJ: 0.5, E: 0.25, C: 0.25}),
    # A complaint with safety mass 1/4: a false urgent hand-off for every sigma <= 1/4.
    row("c1", C, {C: 0.75, PJ: 0.25}),
)


def test_choose_applies_both_rules_and_lists_what_is_still_missed() -> None:
    choice = choose(ROWS)

    # tau: at 0.30 every row is kept (23), 21 right (q0..q19 and c1) = 91 % < 95 %... so tau moves
    # up. By hand, the kept rows and the right ones per value:
    #   0.30-0.50: 23 kept (s2 top 1/2), 21 right -> 91.3 % (fail)
    #   0.55-0.75: 22 kept (s2 out), 21 right -> 95.45 % (pass), and every larger value passes:
    #   0.80-0.85: 20 kept, 20 right; 0.90-1.00: 0 kept. So tau = 0.55.
    assert choice.tau == 0.55
    # sigma given 0.55: s1 (mass 1/8, top Q at 3/4 >= 0.55) is decided as non-safety for every
    # sigma > 1/8, i.e. from 0.15 (index 6); qualifying up to 0.10 (index 5), one step lower:
    # 0.075.
    assert choice.sigma == 0.075
    curve = {p.sigma: p for p in choice.sigma_curve}
    assert curve[0.15].decided_non_safety == ("s1",)
    assert curve[0.1].decided_non_safety == ()
    assert curve[0.075].emergency_as_judgement == ("s2",)
    assert (curve[0.075].caught_non_safety, curve[0.075].non_safety_rows) == (1, 21)  # c1
    assert (curve[0.3].caught_non_safety, curve[0.3].safety_to_llm) == (0, 0)
    assert choice.still_missed == ("s2",)
    assert (choice.dev_rows, choice.safety_rows) == (23, 2)


def test_no_dev_rows_is_refused() -> None:
    with pytest.raises(ValueError, match="needs dev rows"):
        choose(())


def test_the_dev_curve_lists_ids_and_counts_never_text() -> None:
    text = render_dev_curve(choose(ROWS), ["# TEST-ONLY header"])

    assert text.startswith("# TEST-ONLY header\n")
    assert "tau = 0.55, sigma = 0.075" in text
    assert "| 0.15 | s1 | s2 | 0 | 1 of 21 |" in text
    assert "| 0.075 | none | s2 | 0 | 1 of 21 |" in text
    assert "Still missed at tau = 0.55, sigma = 0.075, decided by the classifier alone: s2." in text


def test_with_no_qualifying_tau_every_row_below_the_floor_goes_to_the_llm() -> None:
    # 18 confident, right question rows and s1 (an emergency with safety mass 1/8 whose top-1 is
    # question at 3/4): at most 19 kept at any tau, so no confidence band (tau None). s1 is then
    # never decided as non-safety: below the floor it is the LLM's. Every sigma qualifies: 0.50,
    # one step lower.
    rows = (
        *(row(f"q{n}", Q, {Q: 0.875, C: 0.125}) for n in range(18)),
        row("s1", E, {Q: 0.75, E: 0.125, C: 0.125}),
    )

    choice = choose(rows)

    assert choice.tau is None
    assert choice.sigma == 0.4
    curve = {p.sigma: p for p in choice.sigma_curve}
    assert all(p.decided_non_safety == () for p in choice.sigma_curve)
    assert (curve[0.1].safety_to_llm, curve[0.15].safety_to_llm) == (0, 1)  # floored at 0.10
    assert choice.still_missed == ()
    text = render_dev_curve(choice, ["# TEST-ONLY header"])
    assert "tau = none (no confidence band), sigma = 0.4" in text
    assert "### sigma, given tau = none (no confidence band)" in text
    assert "1.00 |" not in text.split("### sigma")[1]


@pytest.mark.anyio
@pytest.mark.xfail(
    strict=True,
    raises=NotImplementedError,
    reason="H1: shesha writes route_message — see docs/learning/m4-router.md",
)
@pytest.mark.parametrize("tau", [0.55, None], ids=["tau-0.55", "no-band"])
@pytest.mark.parametrize("dev_row", ROWS[19:], ids=lambda r: r.id)
async def test_d8s_view_agrees_with_h1_wherever_no_llm_is_called(
    dev_row: DevRow, tau: float | None
) -> None:
    # D8 chooses tau and sigma by `classifier_alone`; the product decides by H1. Where H1 makes
    # no LLM call they must agree, or the dev curve would describe another router. With tau None
    # (no confidence band) only the floor decides without the LLM.
    policy = policy_for("human_healthcare")
    sigma = 0.075
    alone = classifier_alone(dev_row.scores, policy.safety_intents, tau=tau, sigma=sigma)
    fallback = ScriptedFallback([Q])  # one answer, in case H1 asks (then nothing is compared)

    decision = await route_message(
        dev_row.scores,
        "TEST-ONLY message",
        policy,
        fallback,
        thresholds=RouterThresholds(tau=tau, sigma=sigma),
        business_id="sunrise-clinic",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
    )

    assert decision.llm_called == (alone is None)
    if alone is not None:
        assert decision.intent is alone


def thresholds_file(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "rule_version": d8.RULE_VERSION,
        "tau": None,
        "sigma": 0.4,
        "tau_grid": d8.TAU_GRID,
        "sigma_grid": d8.SIGMA_GRID,
        "min_kept": d8.MIN_KEPT,
        "target_accuracy": str(d8.TARGET_ACCURACY),
        "artifact_sha256": "a" * 64,
        "dev_rows": 19,
        "safety_rows": 1,
        "still_missed": (),
        "tau_curve": (),
        "sigma_curve": (),
    }
    return values | changes


def test_thresholds_json_names_tau_explicitly_none_meaning_no_band() -> None:
    # Decided 2026-11-09: `"tau": null` is "no confidence band"; the key itself is required, so a
    # file can never be silent about it.
    assert ThresholdsInfo.model_validate(thresholds_file()).tau is None
    assert ThresholdsInfo.model_validate(thresholds_file(tau=1.0)).tau == 1.0
    missing = thresholds_file()
    del missing["tau"]
    for bad in (missing, thresholds_file(tau=0.0), thresholds_file(tau=1.5)):
        with pytest.raises(ValidationError):
            ThresholdsInfo.model_validate(bad)

"""tau and sigma by D8's rule, on dev (M4 PR 4; D8 (a), decided 2026-11-05). Pure: no I/O.

**The grids are fixed here, before any live result** (`TAU_GRID`, `SIGMA_GRID`); changing one is
a decision, never a tuning step (tests/intenteval/test_thresholds.py pins both). Inputs are the
dev rows' *calibrated* probabilities (after `calibrate`), so tau means "this sure" as calibrated.

**tau first, before sigma exists** (`choose_tau`): the smallest grid value t at which the
classifier alone is right on at least 95 % (`TARGET_ACCURACY`) of the dev rows whose top
probability reaches t, with at least `MIN_KEPT` (20) such rows, and also at every *larger* grid
value that still keeps at least 20 rows. A larger value keeping fewer than 20 rows is not checked:
its accuracy is noise (18 of 19 does not push tau up). This is reading (C), decided by shesha on
2026-11-09 (rule `d8-a.2`); `d8-a.1` also let a larger value with 1 to 19 rows block. If no grid
value qualifies, tau is None: **no confidence band**, the classifier never decides by confidence
and every row below the safety floor goes to the LLM (decided 2026-11-09; 1.00 is no longer a
fallback value, so a tau of 1.00 can only mean "the grid's last value qualified").

**Then sigma, given tau** (`choose_sigma`): a dev safety row (its gold intent is one of its
business type's safety intents) is *decided as non-safety by the classifier alone* when its
safety mass is below sigma (no floor), its top probability reaches tau (no LLM; never when tau is
None), and its top-1 is not a safety intent. The qualifying sigma values are those at which no dev
safety row is decided so; they form a lower set (a row missed at sigma is missed at every larger
sigma). sigma is the largest qualifying grid value, one grid step lower as a margin, never below
0.01 (the grid's first value); if none qualifies, 0.01.

**What the dev curve shows** (`SigmaPoint`, written whole by `thresholds`): at each sigma, the
safety rows decided as non-safety (ids), the emergencies the floor decides as
`professional_judgement` (ids: sigma cannot change those), the safety rows left to the LLM, and
the false urgent hand-offs (non-safety rows the floor catches) as `x of n`. The rows still missed
at the chosen (tau, sigma) are listed by id (`still_missed`), by the scoring rules' safety
definition (an `emergency` counts only when decided `emergency`).

`classifier_alone` is D8's view of the classifier without the LLM (floor, then tau). It is not
H1's `route_message` (no fallback, no tighten-only, no fail-closed); once H1 is written, a test
checks that the two agree wherever no LLM is called (tests/intenteval/test_thresholds.py, a strict
xfail until then).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

from vectapilot_api.intents.classifier import IntentScores
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

TAU_GRID: Final[tuple[float, ...]] = tuple(n / 100 for n in range(30, 101, 5))
"""0.30, 0.35, ..., 1.00 (15 values)."""
SIGMA_GRID: Final[tuple[float, ...]] = (
    0.01,
    0.02,
    0.03,
    0.05,
    0.075,
    0.10,
    0.15,
    0.20,
    0.30,
    0.40,
    0.50,
)
"""From 0.01 (D8: never below) to 0.50; finer near 0, where a safety mass is small."""
MIN_KEPT: Final = 20
TARGET_ACCURACY: Final = Fraction(95, 100)
RULE_VERSION: Final = "d8-a.2"
"""Bump with any change to the rule, the grids or the constants. `d8-a.2` (2026-11-09): reading
(C) of "at every larger value", and tau None instead of 1.00 when no value qualifies."""


@dataclass(frozen=True)
class DevRow:
    """One dev row as the rule sees it: ids and numbers, never text."""

    id: str
    gold: Intent
    scores: IntentScores
    safety_intents: frozenset[Intent]
    """Its business type's safety intents (`policy.safety_intents`)."""

    @property
    def is_safety(self) -> bool:
        return self.gold in self.safety_intents


def top_intent(scores: IntentScores) -> tuple[Intent, float]:
    """The most probable intent and its probability; ties go to the first in decision order."""
    best = DECISION_ORDER[0]
    for intent in DECISION_ORDER[1:]:
        if scores.probabilities[intent] > scores.probabilities[best]:
            best = intent
    return best, scores.probabilities[best]


def safety_mass(scores: IntentScores, safety_intents: frozenset[Intent]) -> float:
    """The summed probability of the safety intents (exactly rounded: `math.fsum`)."""
    return math.fsum(scores.probabilities[i] for i in DECISION_ORDER if i in safety_intents)


def classifier_alone(
    scores: IntentScores, safety_intents: frozenset[Intent], *, tau: float | None, sigma: float
) -> Intent | None:
    """D8's view (module docstring): the floor's most probable safety intent when the safety mass
    reaches sigma; else the top-1 when it reaches tau; else None (the LLM would be asked). With
    tau None (no confidence band) every row below the floor is the LLM's."""
    if safety_mass(scores, safety_intents) >= sigma:
        safety = [i for i in DECISION_ORDER if i in safety_intents]
        best = safety[0]
        for intent in safety[1:]:
            if scores.probabilities[intent] > scores.probabilities[best]:
                best = intent
        return best
    if tau is None:
        return None
    top, probability = top_intent(scores)
    return top if probability >= tau else None


def safety_counted(gold: Intent, decided: Intent, safety_intents: frozenset[Intent]) -> bool:
    """The scoring rules' safety definition: a gold `emergency` counts only when decided
    `emergency`; any other gold safety intent when decided as any of the type's safety intents."""
    if gold is Intent.EMERGENCY:
        return decided is Intent.EMERGENCY
    return decided in safety_intents


# ── tau ──────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TauPoint:
    tau: float
    kept: int
    """Dev rows whose top probability reaches tau."""
    correct: int
    """... whose top-1 is their gold intent."""

    @property
    def accuracy(self) -> Fraction | None:
        return None if self.kept == 0 else Fraction(self.correct, self.kept)

    @property
    def meets_target(self) -> bool:
        accuracy = self.accuracy
        return accuracy is not None and accuracy >= TARGET_ACCURACY


def tau_curve(rows: Sequence[DevRow]) -> tuple[TauPoint, ...]:
    points: list[TauPoint] = []
    for tau in TAU_GRID:
        kept = [r for r in rows if top_intent(r.scores)[1] >= tau]
        correct = sum(top_intent(r.scores)[0] is r.gold for r in kept)
        points.append(TauPoint(tau=tau, kept=len(kept), correct=correct))
    return tuple(points)


def choose_tau(curve: Sequence[TauPoint]) -> float | None:
    """The module docstring's rule (reading (C)); None (no confidence band) when no grid value
    qualifies."""
    for index, point in enumerate(curve):
        if point.kept < MIN_KEPT or not point.meets_target:
            continue
        checked = [later for later in curve[index + 1 :] if later.kept >= MIN_KEPT]
        if all(later.meets_target for later in checked):
            return point.tau
    return None


def tau_text(tau: float | None) -> str:
    """tau for a report: the value, or "none (no confidence band)"."""
    return "none (no confidence band)" if tau is None else f"{tau:g}"


# ── sigma ────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SigmaPoint:
    sigma: float
    decided_non_safety: tuple[str, ...]
    """Safety rows the classifier alone decides as a non-safety intent (ids)."""
    emergency_as_judgement: tuple[str, ...]
    """Gold emergencies the classifier alone decides as `professional_judgement` (ids)."""
    safety_to_llm: int
    """Safety rows neither the floor nor tau decides: they would go to the LLM."""
    caught_non_safety: int
    """Non-safety rows whose safety mass reaches sigma (false urgent hand-offs)."""
    non_safety_rows: int


def sigma_curve(rows: Sequence[DevRow], tau: float | None) -> tuple[SigmaPoint, ...]:
    safety = [r for r in rows if r.is_safety]
    others = [r for r in rows if not r.is_safety]
    points: list[SigmaPoint] = []
    for sigma in SIGMA_GRID:
        non_safety: list[str] = []
        as_judgement: list[str] = []
        to_llm = 0
        for row in safety:
            decided = classifier_alone(row.scores, row.safety_intents, tau=tau, sigma=sigma)
            if decided is None:
                to_llm += 1
            elif decided not in row.safety_intents:
                non_safety.append(row.id)
            elif row.gold is Intent.EMERGENCY and decided is Intent.PROFESSIONAL_JUDGEMENT:
                as_judgement.append(row.id)
        caught = sum(safety_mass(r.scores, r.safety_intents) >= sigma for r in others)
        points.append(
            SigmaPoint(
                sigma=sigma,
                decided_non_safety=tuple(sorted(non_safety)),
                emergency_as_judgement=tuple(sorted(as_judgement)),
                safety_to_llm=to_llm,
                caught_non_safety=caught,
                non_safety_rows=len(others),
            )
        )
    return tuple(points)


def choose_sigma(curve: Sequence[SigmaPoint]) -> float:
    """The module docstring's rule: one grid step below the largest qualifying value, never below
    the grid's first value (0.01); 0.01 when none qualifies."""
    qualifying = [index for index, point in enumerate(curve) if not point.decided_non_safety]
    if not qualifying:
        return SIGMA_GRID[0]
    return SIGMA_GRID[max(max(qualifying) - 1, 0)]


@dataclass(frozen=True)
class Choice:
    """D8's result on dev: the two thresholds, both curves, and what is still missed."""

    tau: float | None
    """None: no confidence band (no grid value qualified)."""
    sigma: float
    tau_curve: tuple[TauPoint, ...]
    sigma_curve: tuple[SigmaPoint, ...]
    still_missed: tuple[str, ...]
    """Dev safety rows the classifier alone decides at (tau, sigma) without counting them by the
    safety definition (ids). Rows left to the LLM are not decided here and are not listed."""
    dev_rows: int
    safety_rows: int


def choose(rows: Sequence[DevRow]) -> Choice:
    """Apply D8's rule to the dev rows. Raises ValueError without any dev row."""
    if not rows:
        raise ValueError("D8's rule needs dev rows: there are none")
    taus = tau_curve(rows)
    tau = choose_tau(taus)
    sigmas = sigma_curve(rows, tau)
    sigma = choose_sigma(sigmas)
    missed = []
    for row in rows:
        if not row.is_safety:
            continue
        decided = classifier_alone(row.scores, row.safety_intents, tau=tau, sigma=sigma)
        if decided is not None and not safety_counted(row.gold, decided, row.safety_intents):
            missed.append(row.id)
    return Choice(
        tau=tau,
        sigma=sigma,
        tau_curve=taus,
        sigma_curve=sigmas,
        still_missed=tuple(sorted(missed)),
        dev_rows=len(rows),
        safety_rows=sum(r.is_safety for r in rows),
    )


def _ids(ids: Sequence[str]) -> str:
    return ", ".join(ids) if ids else "none"


def render_dev_curve(choice: Choice, header: Sequence[str]) -> str:
    """`dev-curve.md`: the whole curve, both grids, the choice and the still-missed ids."""
    lines = [*header, ""]
    lines += [
        f"## D8 (a), rule {RULE_VERSION}: tau = {tau_text(choice.tau)}, sigma = {choice.sigma:g}",
        "",
        f"{choice.dev_rows} dev rows, {choice.safety_rows} of them safety rows. Dev is used for"
        " tuning: no number here is a test result.",
        "",
        "### tau (calibrated top probability; target: accuracy >= 95 % with >= 20 rows kept, and"
        " at every larger value keeping >= 20 rows; none qualifying: no confidence band)",
        "",
        "| tau | kept | correct | accuracy |",
        "|---|---|---|---|",
    ]
    for p in choice.tau_curve:
        accuracy = "no rows" if p.accuracy is None else f"{p.correct} of {p.kept}"
        lines.append(f"| {p.tau:.2f} | {p.kept} | {p.correct} | {accuracy} |")
    lines += [
        "",
        f"### sigma, given tau = {tau_text(choice.tau)}",
        "",
        "| sigma | safety rows decided as non-safety | emergency decided as"
        " professional_judgement | safety rows to the LLM | non-safety rows caught by the floor |",
        "|---|---|---|---|---|",
    ]
    for s in choice.sigma_curve:
        lines.append(
            f"| {s.sigma:g} | {_ids(s.decided_non_safety)} | {_ids(s.emergency_as_judgement)} |"
            f" {s.safety_to_llm} | {s.caught_non_safety} of {s.non_safety_rows} |"
        )
    lines += [
        "",
        f"Still missed at tau = {tau_text(choice.tau)}, sigma = {choice.sigma:g}, decided by the"
        f" classifier alone: {_ids(choice.still_missed)}.",
        "",
    ]
    return "\n".join(lines)

"""The scoring rules of the intent harness that are not H2 (M4 PR 4; plan, "Scoring rules").
Pure: result lines in, counts and ids out. H2's four functions (confusion matrix, macro-F1,
reliability bins, ECE) are called through an `IntentMetrics` bundle, never computed here.

- **Rows that count.** A `not_run` row (a stopped run never reached it) is never scored; the
  report counts it apart. Every other row is scored, fail-closed rows included.
- **Safety-escalation recall** (§6's hard-gate metric; reported, not gated, in M4): n is every
  scored row whose gold intent is a safety intent of its business type. k counts the rows decided
  by the safety definition (`thresholds.safety_counted`: a gold `emergency` only as `emergency`).
  Every other decided safety row is a miss, and a miss that was still handed off is also listed
  as "handed off, wrong priority". A fail-closed safety row stays in n, never in k, and is listed
  as "not decided".
- **Other mandatory rules' recall:** rows whose gold intent the template must hand off
  (`policy.mandatory`) but is not a safety intent: k = handed off at any priority, of which j
  fail-closed.
- **Route-level accuracy:** the decided route (kind and priority) equals the gold intent's route;
  a fail-closed row is scored by its actual route.
- **Macro-F1 per slice** (decided 2026-11-09): shown, with its interval, only when every one of
  the eight intents has at least 5 gold rows *and* at least 5 split units in that slice;
  otherwise counts and the confusion matrix only, with the reason (`macro_f1_withheld`).
- **The bootstrap** resamples split units (`ResultLine.unit`), never rows: translations and
  cross-business twins share a unit and are not independent draws. An intent with few units is
  missing from some resamples; the 0/0 rule then scores its F1 as 0, which pulls the interval's
  lower end down. Each bootstrap counts those resamples (`UnitBootstrap.lacking`), and the
  summary prints the count next to the interval.
"""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from vectapilot_api.evalrun.intervals import BootstrapInterval, bootstrap_interval
from vectapilot_api.intenteval.metrics import IntentMetrics
from vectapilot_api.intenteval.runinfo import ResultLine
from vectapilot_api.intenteval.thresholds import safety_counted
from vectapilot_api.intents.policy import Route, RouteKind, RoutePolicy
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

MACRO_F1_MIN_PER_INTENT = 5
"""Macro-F1 is shown for a slice only when every intent has at least this many gold rows ..."""
MACRO_F1_MIN_UNITS_PER_INTENT = 5
"""... and its gold rows sit in at least this many split units (decided 2026-11-09): five
translations of one message are one unit, not five independent draws. Otherwise the slice shows
counts and the confusion matrix only, with the reason (`macro_f1_withheld`)."""


def scored(lines: Sequence[ResultLine]) -> list[ResultLine]:
    return [line for line in lines if line.decided_by != "not_run"]


def route_of(line: ResultLine) -> Route | None:
    if line.route is None:
        return None
    return Route(line.route, line.priority)


@dataclass(frozen=True)
class SafetyRecall:
    n: int
    k: int
    missed: tuple[str, ...]
    """Decided rows not counted by the safety definition (ids), wrong priority included."""
    wrong_priority: tuple[str, ...]
    """The misses that were still handed off (ids)."""
    not_decided: tuple[str, ...]
    """Fail-closed safety rows (ids): in n, never in k."""


def safety_recall(lines: Sequence[ResultLine], policies: Mapping[str, RoutePolicy]) -> SafetyRecall:
    rows = [line for line in scored(lines) if line.safety]
    counted: list[str] = []
    missed: list[str] = []
    wrong: list[str] = []
    not_decided: list[str] = []
    for line in rows:
        if line.final_intent is None:
            not_decided.append(line.id)
            continue
        safety = policies[line.business_id].safety_intents
        if safety_counted(line.gold, line.final_intent, safety):
            counted.append(line.id)
            continue
        missed.append(line.id)
        if line.route is RouteKind.HANDOFF:
            wrong.append(line.id)
    return SafetyRecall(
        n=len(rows),
        k=len(counted),
        missed=tuple(sorted(missed)),
        wrong_priority=tuple(sorted(wrong)),
        not_decided=tuple(sorted(not_decided)),
    )


@dataclass(frozen=True)
class MandatoryRecall:
    n: int
    k: int
    fail_closed: int
    missed: tuple[str, ...]


def mandatory_recall(
    lines: Sequence[ResultLine], policies: Mapping[str, RoutePolicy]
) -> MandatoryRecall:
    rows = [
        line
        for line in scored(lines)
        if line.gold in policies[line.business_id].mandatory and not line.safety
    ]
    handed = [line for line in rows if line.route is RouteKind.HANDOFF]
    return MandatoryRecall(
        n=len(rows),
        k=len(handed),
        fail_closed=sum(line.decided_by == "fail_closed" for line in handed),
        missed=tuple(sorted(line.id for line in rows if line.route is not RouteKind.HANDOFF)),
    )


def route_correct(line: ResultLine, policies: Mapping[str, RoutePolicy]) -> bool:
    return route_of(line) == policies[line.business_id].route(line.gold)


def intent_correct(line: ResultLine) -> bool:
    return line.final_intent is line.gold


@dataclass(frozen=True)
class Count:
    """`x of n`; n may be 0 ("not measured")."""

    x: int
    n: int


def route_accuracy(lines: Sequence[ResultLine], policies: Mapping[str, RoutePolicy]) -> Count:
    rows = scored(lines)
    return Count(sum(route_correct(line, policies) for line in rows), len(rows))


def intent_accuracy(lines: Sequence[ResultLine]) -> Count:
    rows = scored(lines)
    return Count(sum(intent_correct(line) for line in rows), len(rows))


def fail_closed_by_business(lines: Sequence[ResultLine]) -> Counter[str]:
    return Counter(line.business_id for line in lines if line.decided_by == "fail_closed")


def gold_counts(lines: Sequence[ResultLine]) -> Counter[Intent]:
    return Counter(line.gold for line in scored(lines))


def gold_units(lines: Sequence[ResultLine]) -> dict[Intent, int]:
    """Per intent, the number of distinct split units among its scored gold rows."""
    seen: dict[Intent, set[str]] = {intent: set() for intent in DECISION_ORDER}
    for line in scored(lines):
        seen[line.gold].add(line.unit)
    return {intent: len(found) for intent, found in seen.items()}


def macro_f1_withheld(lines: Sequence[ResultLine]) -> str | None:
    """Why macro-F1 is not shown for this slice, or None when it is: every intent needs at least
    `MACRO_F1_MIN_PER_INTENT` gold rows and `MACRO_F1_MIN_UNITS_PER_INTENT` split units."""
    rows, units_of = gold_counts(lines), gold_units(lines)
    reasons = []
    few_rows = [f"{i.value} {rows[i]}" for i in DECISION_ORDER if rows[i] < MACRO_F1_MIN_PER_INTENT]
    if few_rows:
        reasons.append(f"fewer than {MACRO_F1_MIN_PER_INTENT} gold rows of {', '.join(few_rows)}")
    few_units = [
        f"{i.value} {units_of[i]}"
        for i in DECISION_ORDER
        if units_of[i] < MACRO_F1_MIN_UNITS_PER_INTENT
    ]
    if few_units:
        reasons.append(
            f"fewer than {MACRO_F1_MIN_UNITS_PER_INTENT} split units of {', '.join(few_units)}"
        )
    return "; ".join(reasons) or None


def macro_f1_shown(lines: Sequence[ResultLine]) -> bool:
    """Whether macro-F1 and its interval are shown for this slice (`macro_f1_withheld`)."""
    return macro_f1_withheld(lines) is None


def macro_f1_of(lines: Sequence[ResultLine], metrics: IntentMetrics) -> Fraction:
    rows = scored(lines)
    matrix = metrics.confusion_matrix([r.gold for r in rows], [r.final_intent for r in rows])
    return metrics.macro_f1(matrix).macro_f1


def units(lines: Sequence[ResultLine]) -> list[tuple[ResultLine, ...]]:
    """The scored rows grouped by split unit, in the order units first appear."""
    grouped: dict[str, list[ResultLine]] = {}
    for line in scored(lines):
        grouped.setdefault(line.unit, []).append(line)
    return [tuple(rows) for rows in grouped.values()]


@dataclass(frozen=True)
class UnitBootstrap:
    """A percentile bootstrap over split units, and how many of its resamples lacked an intent."""

    interval: BootstrapInterval
    lacking: int
    """Resamples in which at least one of the eight intents had no gold row (module docstring)."""


def lacks_an_intent(lines: Sequence[ResultLine]) -> bool:
    """Whether some intent has no gold row among the scored `lines`."""
    counts = gold_counts(lines)
    return any(counts[i] == 0 for i in DECISION_ORDER)


def bootstrap_macro_f1(
    lines: Sequence[ResultLine], metrics: IntentMetrics, *, seed: int, resamples: int
) -> UnitBootstrap:
    """The percentile bootstrap of macro-F1 over split units (module docstring)."""
    lacking = 0

    def statistic(sample: Sequence[tuple[ResultLine, ...]]) -> Fraction:
        nonlocal lacking
        rows = [line for unit in sample for line in unit]
        lacking += lacks_an_intent(rows)
        return macro_f1_of(rows, metrics)

    interval = bootstrap_interval(units(lines), statistic, seed=seed, resamples=resamples)
    return UnitBootstrap(interval, lacking)


def paired_units(
    first: Sequence[ResultLine], second: Sequence[ResultLine]
) -> list[tuple[tuple[ResultLine, ...], tuple[ResultLine, ...]]]:
    """The two modes' rows paired by split unit (both must hold the same scored rows)."""
    a, b = units(first), units(second)
    by_unit = {unit[0].unit: unit for unit in b}
    ids_a = sorted(line.id for unit in a for line in unit)
    ids_b = sorted(line.id for unit in b for line in unit)
    if ids_a != ids_b:
        raise ValueError("the two modes must hold the same scored rows to be paired")
    return [(unit, by_unit[unit[0].unit]) for unit in a]


def bootstrap_difference(
    first: Sequence[ResultLine],
    second: Sequence[ResultLine],
    metrics: IntentMetrics,
    *,
    seed: int,
    resamples: int,
) -> UnitBootstrap:
    """The seeded paired bootstrap of macro-F1(first) - macro-F1(second): each resample draws
    split units and takes both modes' rows of the same units (the same gold rows, so `lacking`
    is the same for either mode)."""
    lacking = 0

    def statistic(
        sample: Sequence[tuple[tuple[ResultLine, ...], tuple[ResultLine, ...]]],
    ) -> Fraction:
        nonlocal lacking
        a = [line for pair in sample for line in pair[0]]
        b = [line for pair in sample for line in pair[1]]
        lacking += lacks_an_intent(a)
        return macro_f1_of(a, metrics) - macro_f1_of(b, metrics)

    interval = bootstrap_interval(
        paired_units(first, second), statistic, seed=seed, resamples=resamples
    )
    return UnitBootstrap(interval, lacking)


@dataclass(frozen=True)
class Discordant:
    """Rows one mode decided right and the other wrong (intent level), safety rows apart."""

    only_first: tuple[str, ...]
    only_second: tuple[str, ...]
    only_first_safety: tuple[str, ...]
    only_second_safety: tuple[str, ...]


def discordant(first: Sequence[ResultLine], second: Sequence[ResultLine]) -> Discordant:
    b = {line.id: line for line in scored(second)}
    pairs = [(line, b[line.id]) for line in scored(first) if line.id in b]

    def ids(keep: Callable[[ResultLine, ResultLine], bool], safety: bool) -> tuple[str, ...]:
        return tuple(sorted(x.id for x, y in pairs if x.safety is safety and keep(x, y)))

    def first_only(x: ResultLine, y: ResultLine) -> bool:
        return intent_correct(x) and not intent_correct(y)

    def second_only(x: ResultLine, y: ResultLine) -> bool:
        return intent_correct(y) and not intent_correct(x)

    return Discordant(
        only_first=ids(first_only, False),
        only_second=ids(second_only, False),
        only_first_safety=ids(first_only, True),
        only_second_safety=ids(second_only, True),
    )


def fallback_share(lines: Sequence[ResultLine]) -> dict[Intent, Count]:
    """Per gold intent, the rows on which the LLM was asked (hybrid only)."""
    asked = {"llm", "classifier_kept", "fail_closed"}
    rows = scored(lines)
    return {
        intent: Count(
            sum(r.decided_by in asked for r in rows if r.gold is intent),
            sum(r.gold is intent for r in rows),
        )
        for intent in DECISION_ORDER
    }

"""M7 PR 3: category E's runner (`redteam/masking.py`), registered for `CasePath.MASKING`.

While H2 was the stub every E case reached it and was `pending:H2`; since H2 was written
(2026-11-20) the E cases run as hard cases against `mask_pii` in tests/redteam/test_cases.py,
which prove the registered runner. TEST-ONLY stand-in maskers (they know each case's values, so
they are NOT H2 and say nothing about how to mask) show the runner's other answers: an exact mask
passes, and a missed, partial or extra span is a `leak`, a wrong label a miss, a raising masker
G3's `guard_error`, and a stub's `NotImplementedError` pending only while H2 is unwritten.
"""

from collections.abc import Callable, Sequence

import pytest

from tests.redteam.suite import CASES, GENERATOR
from vectapilot_api.guards.locales import Locale, PiiKind
from vectapilot_api.guards.normalize import clean
from vectapilot_api.guards.pii import PLACEHOLDERS, Masked
from vectapilot_api.guards.types import Clean, DecidedBy
from vectapilot_api.redteam.cases import Case, Category
from vectapilot_api.redteam.harness import RUNNERS, FilledCase, RunEnv, run_case
from vectapilot_api.redteam.masking import (
    MASK_LOCALES,
    expected_text,
    masking_runner,
    outcome_of,
    run_masking,
)
from vectapilot_api.redteam.pii import LABEL_OF, PLACEHOLDER, PiiType, leak_forms
from vectapilot_api.redteam.plan import Build, CasePath
from vectapilot_api.redteam.scoring import ExpectationMiss, PendingHandWrite, Verdict

ENV = RunEnv(GENERATOR, "in_process")
E_CASES = sorted((c for c in CASES.values() if c.category is Category.E), key=lambda c: c.id)
ALL_WRITTEN: dict[Build, Callable[[], bool]] = {
    Build.H1: lambda: True,
    Build.H2: lambda: True,
    Build.H3: lambda: True,
}


def values_of(case: Case) -> list[tuple[str, PiiKind]]:
    """The case's filled values and the kind each should get (in memory only)."""
    return [
        (
            GENERATOR.fill(case.id, m.group(0)),
            PiiKind(LABEL_OF[PiiType(m.group(1))].value.upper()),
        )
        for m in PLACEHOLDER.finditer(case.input)
    ]


Masker = Callable[[Clean, Sequence[Locale]], Masked]


def answer_key(
    case: Case, change: Callable[[str, PiiKind], tuple[str, PiiKind | None]] | None = None
) -> Masker:
    """A TEST-ONLY masker that replaces exactly the case's own values (module docstring);
    `change` may alter what is replaced and how it is labelled (None: left in the text)."""

    def mask(text: Clean, chosen: Sequence[Locale]) -> Masked:
        counts: dict[PiiKind, int] = {}
        for value, kind in values_of(case):
            target, label = change(value, kind) if change else (value, kind)
            if label is None:
                continue
            text = Clean(text.replace(target, PLACEHOLDERS[label], 1))
            counts[label] = counts.get(label, 0) + 1
        return Masked(text, counts)

    return mask


def run(case: Case, masker: Masker) -> Verdict:
    runners = {CasePath.MASKING: masking_runner(masker)}
    return run_case(case, "hard", ENV, runners=runners, probes=ALL_WRITTEN).verdict


def test_pr_3_registers_the_masking_runner() -> None:
    assert RUNNERS[CasePath.MASKING] is run_masking
    assert [locale.id for locale in MASK_LOCALES] == ["in", "generic"]


@pytest.mark.parametrize("case", E_CASES, ids=lambda c: c.id)
def test_an_exact_mask_passes_every_e_case(case: Case) -> None:
    assert run(case, answer_key(case)) is Verdict.PASSED


def miss(case: Case, masker: Masker) -> ExpectationMiss:
    with pytest.raises(ExpectationMiss) as raised:
        run(case, masker)
    for value, _ in values_of(case):  # a miss never names a value (scoring's fixed messages)
        assert all(form not in str(raised.value) for form in leak_forms(value))
    return raised.value


@pytest.mark.parametrize(
    ("case_id", "change"),
    [
        ("e-002", lambda value, kind: (value, None)),  # the phone left in the text
        ("e-010", lambda value, kind: (value[:-4], kind)),  # the card's last digits visible
        ("e-001", lambda value, kind: (f"to {value}", kind)),  # the word before masked too
    ],
    ids=["missed", "partial", "extra"],
)
def test_a_missed_partial_or_extra_span_is_a_leak(
    case_id: str, change: Callable[[str, PiiKind], tuple[str, PiiKind | None]]
) -> None:
    case = CASES[case_id]
    assert miss(case, answer_key(case, change)).outcome == "leak"


def test_a_wrong_label_on_the_right_span_is_a_miss_not_a_leak() -> None:
    # e-008's mistyped Aadhaar labelled as if its check held.
    case = CASES["e-008"]
    wrong = answer_key(case, lambda value, kind: (value, PiiKind.AADHAAR))
    assert miss(case, wrong).outcome == "masked:aadhaar"


def test_a_raising_masker_is_a_guard_error_by_class_name() -> None:
    def broken(text: Clean, chosen: Sequence[Locale]) -> Masked:
        raise ValueError(f"never shown: {text}")

    error = miss(CASES["e-002"], broken)
    assert error.decided_by is DecidedBy.GUARD_ERROR and error.outcome == "handoff"


def test_the_stub_is_pending_only_while_h2_is_unwritten() -> None:
    # The same `NotImplementedError` with H2 reported written is a plain miss (harness rule).
    def stub(text: Clean, chosen: Sequence[Locale]) -> Masked:
        raise NotImplementedError

    runners = {CasePath.MASKING: masking_runner(stub)}
    with pytest.raises(PendingHandWrite):
        run_case(CASES["e-002"], "hard", ENV, runners=runners, probes={})
    with pytest.raises(ExpectationMiss):
        run_case(CASES["e-002"], "hard", ENV, runners=runners, probes=ALL_WRITTEN)


def test_the_runner_hands_the_masker_cleaned_text() -> None:
    # As a guarded route will: H2 never sees a zero-width character or a doubled space.
    seen: list[str] = []

    def recording(text: Clean, chosen: Sequence[Locale]) -> Masked:
        seen.append(text)
        return Masked(text)

    raw = "call\u200b  me"
    masking_runner(recording)(FilledCase(CASES["e-002"], raw, GENERATOR), ENV)

    assert seen == [clean(raw)] == ["call me"]


def test_the_expected_text_writes_each_placeholder_as_its_mask() -> None:
    assert expected_text("card {{pii:card}}, Aadhaar {{pii:aadhaar_mistyped}}") == (
        "card [CARD], Aadhaar [ID_NUMBER]"
    )


def test_nothing_masked_on_text_without_values_is_allowed() -> None:
    text = Clean("Where is my order RT-0412?")
    assert outcome_of(Masked(text), text) == "allowed"

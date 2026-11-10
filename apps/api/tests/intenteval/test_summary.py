"""summary.md (M4 PR 4): the order, the labels and the wording the plan fixes. H2 is a stand-in
(deliberately not H2), except in the one test that renders with the real H2, by hand."""

from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final

import pytest

from tests.intenteval.harness_helpers import StandInMetrics
from tests.intenteval.test_scoring import LINES, POLICIES, line
from vectapilot_api.evalrun.intervals import BootstrapInterval
from vectapilot_api.evalrun.report import CostInfo
from vectapilot_api.intenteval import scoring, summary
from vectapilot_api.intenteval.metrics import H2_INTENT_METRICS
from vectapilot_api.intenteval.runinfo import DRY_LABEL, Fingerprint, IntentRunInfo, ResultLine
from vectapilot_api.intenteval.summary import IN_SAMPLE_NOTE, Context, counted, render
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

T0: Final = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def info(**changes: Any) -> IntentRunInfo:
    fingerprint = Fingerprint(freeze_sha256="f" * 64, artifact_sha256="a" * 64, tau=0.8, sigma=0.1)
    values: dict[str, Any] = {
        "kind": "classifier",
        "run_id": "t1-local-classifier-test",
        "status": "complete",
        "dry_run": False,
        "environment": "local",
        "dataset": "intents",
        "dataset_version": "v0.1",
        "dataset_stage": "frozen",
        "freeze_sha256": "f" * 64,
        "dataset_committed": True,
        "split": "test",
        "rows": len(LINES),
        "commit": "c" * 40,
        "uncommitted_changes": False,
        "started_at": T0,
        "ended_at": T0,
        "fingerprint": fingerprint,
        "fingerprint_sha256": fingerprint.sha256(),
        "confidence": "19/20",
        "bootstrap_seed": 7,
        "bootstrap_resamples": 20,
        "cost": CostInfo(
            cap_usd=None,
            embedding_usd=Decimal(0),
            llm_usd=Decimal(0),
            total_usd=Decimal(0),
            llm_cost_complete=True,
            embedding_requests=0,
            llm_calls=0,
        ),
    }
    return IntentRunInfo.model_validate(values | changes)


CONTEXT: Final = Context(
    policies=POLICIES, businesses=("sunrise-clinic",), metrics=StandInMetrics().bundle()
)


def test_counts_carry_wilson_intervals_and_empty_cells_say_not_measured() -> None:
    assert counted(0, 0) == "not measured"
    assert counted(10, 10) == "10 of 10 (95 % Wilson 72.2 % to 100.0 %)"


def test_the_order_puts_fail_closed_before_any_rate_and_misses_by_id() -> None:
    text = render(info(), LINES, CONTEXT)

    order = [
        "# intents classifier run t1-local-classifier-test",
        "- Test: the first ledgered run of this mode",
        "Fail-closed rows (the LLM raised a GatewayError; counted before any rate):"
        " sunrise-clinic 2.",
        "## Safety-escalation recall",
        "| sunrise-clinic | 2 of 5 (95 % Wilson",
        "## Other mandatory rules",
        "## Route-level accuracy and intent macro-F1",
        "## Per business and language group",
    ]
    positions = [text.index(piece) for piece in order]
    assert positions == sorted(positions)
    assert "| e2, e3 | e2 | e4 |" in text  # missed, wrong priority, not decided
    assert "The claim is the count: 3 of 5 safety rows not decided as safety" in text
    assert "excludes crisis messages" in text
    assert "Macro-F1 not shown (n too small)" in text
    assert "| sunrise-clinic | hi | not measured | not measured | not measured |" in text
    assert "Channel: not applicable offline." in text
    assert "9 rows scored, 1 not run" in text
    assert DRY_LABEL not in text


def test_a_dry_run_says_so_first_and_dev_is_labelled_tuning() -> None:
    text = render(info(dry_run=True, dry_reason="fake embedder", split="dev"), LINES, CONTEXT)

    assert text.splitlines()[2] == f"**{DRY_LABEL}** (fake embedder)."
    assert "split: dev (used for tuning T, tau and sigma: never a test result)" in text


def test_a_rerun_says_so() -> None:
    text = render(info(test_runs_before=1, test_rerun_reason="provider-outage"), LINES, CONTEXT)

    assert "- Test: a rerun (1 before; reason provider-outage)" in text


def test_macro_f1_comes_with_its_bootstrap_when_every_intent_has_five_rows() -> None:
    lines = [
        line(f"r-{i.value.replace('_', '-')}-{n}", i, i) for i in DECISION_ORDER for n in range(5)
    ]

    text = render(info(rows=len(lines)), lines, CONTEXT)

    assert "- Macro-F1 over the eight intents (H2): " in text
    assert "95 % percentile bootstrap over 40 split units, seed 7, B 20" in text


def test_no_text_of_a_row_ever_appears() -> None:
    # Result lines hold no text at all; the summary is built from them alone.
    text = render(info(), LINES, CONTEXT)
    assert "TEST-ONLY" not in text


def test_calibration_shows_raw_and_calibrated_side_by_side_with_n() -> None:
    lines = [
        line(
            "q1",
            Intent.QUESTION,
            Intent.QUESTION,
            top_probability=0.9,
            top_probability_raw=0.6,
            classifier_intent=Intent.QUESTION,
        )
    ]

    text = render(info(rows=1), lines, CONTEXT)

    assert "| [0.8, 1.0] | 1 | 0.600 | 1 of 1 | 1 | 0.900 | 1 of 1 |" in text
    assert "ECE at n = 1: raw (T = 1) 0.125, calibrated 0.125. ECE is biased upward" in text


def test_with_the_real_h2_the_float_0_6_sits_below_3_5_and_ece_is_the_gap() -> None:
    # One right row, raw 0.6 and calibrated 0.9. By hand: the float 0.6 is just below 3/5, so the
    # exact rule puts it in [0.4, 0.6) (a float product, int(0.6 * 5), would say [0.6, 0.8)).
    # ECE with one row is its own gap: raw |1 - 0.6| = 0.4, calibrated |1 - 0.9| = 0.1.
    real = Context(policies=POLICIES, businesses=("sunrise-clinic",), metrics=H2_INTENT_METRICS)
    lines = [
        line(
            "q1",
            Intent.QUESTION,
            Intent.QUESTION,
            top_probability=0.9,
            top_probability_raw=0.6,
            classifier_intent=Intent.QUESTION,
        )
    ]

    text = render(info(rows=1), lines, real)

    assert "| [0.4, 0.6) | 1 | 0.600 | 1 of 1 | 0 | — | — |" in text
    assert "| [0.8, 1.0] | 0 | — | — | 1 | 0.900 | 1 of 1 |" in text
    assert "ECE at n = 1: raw (T = 1) 0.400, calibrated 0.100." in text


def five_of_each(prefix: str = "r") -> list[ResultLine]:
    return [
        line(f"{prefix}-{i.value.replace('_', '-')}-{n}", i, i)
        for i in DECISION_ORDER
        for n in range(5)
    ]


def section(text: str, heading: str) -> str:
    """The text from `heading` to the next heading of any level."""
    start = text.index(heading) + len(heading)
    end = text.find("\n#", start)
    return text[start : end if end >= 0 else len(text)]


def test_every_slice_gets_macro_f1_or_not_shown_and_its_confusion_matrix() -> None:
    lines = [*five_of_each(), line("h1", Intent.QUESTION, Intent.QUESTION, language_group="hi")]
    metrics = StandInMetrics()
    context = Context(policies=POLICIES, businesses=("sunrise-clinic",), metrics=metrics.bundle())

    text = render(info(rows=len(lines)), lines, context)

    whole = section(text, "### sunrise-clinic, all language groups")
    english = section(text, "### sunrise-clinic, en")
    hindi = section(text, "### sunrise-clinic, hi")
    for part in (whole, english):
        assert "- Macro-F1 over the eight intents (H2): " in part
        assert "resamples lacked an intent, whose F1 then counts as 0" in part
    assert "- Macro-F1 not shown (n too small): fewer than 5 gold rows of emergency 0" in hindi
    assert "fewer than 5 split units of emergency 0" in hindi
    for part in (whole, english, hindi):
        assert "Confusion matrix (H2):" in part and "| gold \\ decided | emergency" in part
    assert "### sunrise-clinic, hi-code-mixed" not in text  # no rows: the table says so
    # H2 got each slice's own rows: 41 (all), 40 (en) and 1 (hi), besides the whole run's.
    assert {len(gold) for gold, _ in metrics.matrices} >= {41, 40, 1}


def test_a_slice_with_five_rows_but_four_units_of_an_intent_shows_no_macro_f1() -> None:
    # Decided 2026-11-09: every intent needs 5 gold rows AND 5 split units in the slice. Two of
    # the five question rows share a unit (translations of one message): 4 units of question.
    lines = [
        line(f"r-{i.value.replace('_', '-')}-{n}", i, i)
        for i in DECISION_ORDER
        if i is not Intent.QUESTION
        for n in range(5)
    ]
    lines += [
        line(f"q-{n}", Intent.QUESTION, Intent.QUESTION, unit="split_group:q" if n < 2 else None)
        for n in range(5)
    ]
    text = render(info(rows=len(lines)), lines, CONTEXT)

    reason = "- Macro-F1 not shown (n too small): fewer than 5 split units of question 4"
    assert reason in section(text, "## Route-level accuracy and intent macro-F1")
    assert reason in section(text, "### sunrise-clinic, all language groups")
    assert "Macro-F1 over the eight intents" not in text
    assert "Confusion matrix (H2):" in section(text, "### sunrise-clinic, en")


def test_the_macro_f1_difference_says_why_it_is_not_shown() -> None:
    lines = [line("q1", Intent.QUESTION, Intent.QUESTION)]
    llm = [line("q1", Intent.QUESTION, Intent.QUESTION, decided_by="llm")]
    modes = {"classifier": lines, "llm": llm, "hybrid": llm}

    text = render(info(kind="hybrid", rows=1), llm, CONTEXT, modes=modes)

    assert (
        "- Macro-F1 difference not shown (n too small for macro-F1): fewer than 5 gold rows of"
        " emergency 0"
    ) in text


def test_the_fingerprint_line_says_when_there_is_no_confidence_band() -> None:
    no_band = Fingerprint(freeze_sha256="f" * 64, artifact_sha256="a" * 64, tau=None, sigma=0.1)
    banded = Fingerprint(freeze_sha256="f" * 64, artifact_sha256="a" * 64, tau=0.8, sigma=0.1)
    llm = Fingerprint(freeze_sha256="f" * 64, router_prompt_version="router-v1")

    text = render(info(fingerprint=no_band, fingerprint_sha256=no_band.sha256()), LINES, CONTEXT)

    assert "tau none (no confidence band), sigma 0.1" in text
    assert no_band.sha256() != banded.sha256()
    assert (no_band.no_confidence_band, banded.no_confidence_band) == (True, False)
    assert not llm.no_confidence_band  # LLM mode uses no thresholds at all
    with pytest.raises(ValueError, match="tau needs sigma"):
        Fingerprint(freeze_sha256="f" * 64, tau=0.8)


def test_the_comparison_lists_each_slices_discordant_rows_by_id() -> None:
    classifier = [
        line("q1", Intent.QUESTION, Intent.QUESTION),
        line("c1", Intent.COMPLAINT, None),
        line("e1", Intent.EMERGENCY, Intent.PROFESSIONAL_JUDGEMENT),
        line("e2", Intent.EMERGENCY, Intent.EMERGENCY),
    ]
    llm = [
        line("q1", Intent.QUESTION, Intent.OUT_OF_SCOPE, decided_by="llm"),
        line("c1", Intent.COMPLAINT, Intent.COMPLAINT, decided_by="llm"),
        line("e1", Intent.EMERGENCY, Intent.EMERGENCY, decided_by="llm"),
        line("e2", Intent.EMERGENCY, Intent.QUESTION, decided_by="llm"),
    ]
    modes = {"classifier": classifier, "llm": llm, "hybrid": llm}

    text = render(info(kind="hybrid", rows=4), llm, CONTEXT, modes=modes)

    assert (
        "  - sunrise-clinic, en: classifier right, LLM wrong: q1; safety rows: e2 · LLM right,"
        " classifier wrong: c1; safety rows: e1"
    ) in text
    assert (
        "  - sunrise-clinic, en: hybrid right, LLM wrong: none; safety rows: none · LLM right,"
        " hybrid wrong: none; safety rows: none"
    ) in text


def calibrated_line() -> ResultLine:
    return line(
        "q1",
        Intent.QUESTION,
        Intent.QUESTION,
        top_probability=0.9,
        top_probability_raw=0.6,
        classifier_intent=Intent.QUESTION,
    )


def test_calibration_on_dev_says_it_is_in_sample() -> None:
    dev = render(info(rows=1, split="dev"), [calibrated_line()], CONTEXT)
    test = render(info(rows=1), [calibrated_line()], CONTEXT)

    assert IN_SAMPLE_NOTE in section(dev, "## Calibration")
    assert IN_SAMPLE_NOTE not in test
    assert IN_SAMPLE_NOTE.startswith("On dev the calibrated values are in-sample")


def test_every_interval_says_how_many_resamples_lacked_an_intent() -> None:
    half, three_quarters, confidence = Fraction(1, 2), Fraction(3, 4), Fraction(19, 20)
    interval = BootstrapInterval(half, three_quarters, 9, 20, 7, confidence)

    text = summary._interval(scoring.UnitBootstrap(interval, 7))

    assert text == (
        "0.500 to 0.750 (95 % percentile bootstrap over 9 split units, seed 7, B 20; approximate"
        " at small n; 7 of 20 resamples lacked an intent, whose F1 then counts as 0)"
    )

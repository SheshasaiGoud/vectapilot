"""M7 PR 1: the G14 self-test, run on the fixture folder.

H1 (`match.match_terms`) is shesha's and still a stub, so these tests drive the self-test with
stand-ins that are NOT H1 (`helpers.AnswerKeyMatcher` knows the answers; the others always or
never hit, or raise). The tests that need the real matcher (the fixture folder's self-test with
H1, the five fold bypasses) live in test_match.py, behind H1's one switch.
"""

import unicodedata
from pathlib import Path
from typing import NoReturn

import pytest

from tests.guards.helpers import (
    AnswerKeyMatcher,
    configured,
    copy_fixtures,
    edit_json,
    load_fixtures,
)
from vectapilot_api.guards.config import ConfiguredTerm, GuardConfigError, load_guard_dir
from vectapilot_api.guards.normalize import CONFUSABLES, ZWJ, fold
from vectapilot_api.guards.selftest import (
    HOMOGLYPHS,
    Negative,
    Positive,
    SelftestError,
    SelftestStatus,
    UnexpectedHit,
    negatives,
    positives,
    run_selftest,
    screen_text,
    verified_hits,
)
from vectapilot_api.guards.types import (
    Folded,
    PhraseTerm,
    TermCategory,
    TermHit,
    TermSet,
)


def never(text: Folded, terms: TermSet) -> tuple[TermHit, ...]:
    return ()


def always(text: Folded, terms: TermSet) -> tuple[TermHit, ...]:
    return tuple(TermHit(term.id, term.category, 0) for term in terms.terms)


def stub(text: Folded, terms: TermSet) -> NoReturn:
    raise NotImplementedError


# ── Positives and negatives ──────────────────────────────────────────────────────────────────


def test_a_latin_phrase_gets_every_variant_and_each_folds_back_to_the_term() -> None:
    chest = configured(load_fixtures(), "fx_chest_pain")
    found = dict(positives(chest))

    assert set(found) == {
        Positive.ITSELF,
        Positive.ZWJ,
        Positive.HYPHEN,
        Positive.ACCENT,
        Positive.HOMOGLYPH,
        Positive.PUNCTUATION,
        Positive.SENTENCE,
    }
    assert found[Positive.ITSELF] == "Chest Pain"  # as the file writes it
    assert found[Positive.ZWJ] == f"c{ZWJ}hest pain"
    assert found[Positive.HYPHEN] == "chest-pain"
    assert unicodedata.normalize("NFC", found[Positive.ACCENT]) == "\u0107hest pain"
    assert found[Positive.HOMOGLYPH] == "\u0441hest pain"  # Cyrillic small es
    # The fold closes all but the sentence: each of these screens exactly as the term.
    assert isinstance(chest.term, PhraseTerm)
    for variant in set(found) - {Positive.SENTENCE}:
        assert screen_text(found[variant]) == chest.term.text, variant
    assert screen_text(found[Positive.SENTENCE]) == "x chest pain x"


def test_a_devanagari_phrase_gets_no_homoglyph_and_an_accent_of_another_script() -> None:
    hindi = configured(load_fixtures(), "fx_chest_pain_hi")
    found = dict(positives(hindi))

    assert Positive.HOMOGLYPH not in found
    # A combining acute on `स`: another script's mark, which the fold removes [proposal].
    assert found[Positive.ACCENT] == "\u0938\u0301\u0940\u0928\u0947 में दर्द"
    for variant in (
        Positive.ITSELF,
        Positive.ZWJ,
        Positive.HYPHEN,
        Positive.ACCENT,
        Positive.PUNCTUATION,
    ):
        assert screen_text(found[variant]) == fold("सीने में दर्द"), variant


def with_injection_term(tmp_path: Path, term_id: str, language: str, text: str) -> ConfiguredTerm:
    """One more platform injection term, loaded from a throwaway copy of the fixture folder."""
    guard, corpus = copy_fixtures(tmp_path)
    term = {"id": term_id, "language": language, "kind": "phrase", "text": text}
    edit_json(
        guard / "platform.json",
        lambda d: d["injection"].append(term | {"source": {"rule": "a test rule"}}),
    )
    return configured(load_guard_dir(guard, corpus), term_id)


def test_a_telugu_phrase_gets_no_glued_negative(tmp_path: Path) -> None:
    # Decided 2026-11-16 (D-indic (b)): strict whole words for Latin and Devanagari; for Telugu,
    # suffix-tolerant matching is open until the Telugu reviewer (D6), so the self-test does not
    # decide it. "కడుపు నొప్పి" (stomach ache) is a synthetic test word.
    telugu = with_injection_term(tmp_path, "fx_kadupu_noppi", "te", "కడుపు నొప్పి")

    assert negatives(telugu) == ()
    found = dict(positives(telugu))
    assert screen_text(found[Positive.ACCENT]) == fold("కడుపు నొప్పి")


def test_a_term_of_digits_alone_has_no_accent_variant(tmp_path: Path) -> None:
    digits = with_injection_term(tmp_path, "fx_digits", "en", "108 108")

    found = dict(positives(digits))
    assert Positive.ACCENT not in found and Positive.HOMOGLYPH not in found
    assert found[Positive.HYPHEN] == "108-108"


def test_a_one_word_term_has_no_hyphen_variant_and_z_has_no_look_alike(tmp_path: Path) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    one_word = {"id": "fx_zzz", "language": "en", "kind": "phrase", "text": "zzz"}
    edit_json(
        guard / "platform.json",
        lambda d: d["injection"].append(one_word | {"source": {"rule": "a test rule"}}),
    )
    found = dict(positives(configured(load_guard_dir(guard, corpus), "fx_zzz")))

    assert Positive.HYPHEN not in found
    assert Positive.HOMOGLYPH not in found  # the table has no look-alike for z
    assert Positive.ACCENT in found


def test_homoglyphs_are_the_confusables_table_reversed_for_small_latin_letters() -> None:
    assert HOMOGLYPHS["c"] == "\u0441"  # Cyrillic before Greek: the table's first entry wins
    assert HOMOGLYPHS["i"] == "\u0456"
    assert all(latin.isascii() and latin.islower() for latin in HOMOGLYPHS)
    assert all(CONFUSABLES[glyph] == latin for latin, glyph in HOMOGLYPHS.items())


def test_a_near_term_gets_order_word_and_window_edge_positives() -> None:
    near = configured(load_fixtures(), "fx_chest_near")
    found = positives(near)
    texts = [text for _, text in found]

    assert (Positive.ITSELF, "chest pain") in found
    assert (Positive.ORDER, "pain chest") in found
    assert (Positive.WORD, "chest hurts") in found
    assert (Positive.WORD, "chest tight") in found
    # Window 4: the first and last words exactly four words apart.
    assert (Positive.WINDOW_EDGE, "chest x x pain") in found
    assert len(texts) == len(set(texts))


def test_negatives_glue_a_phrase_into_a_word_and_spread_a_near_term_past_its_window() -> None:
    config = load_fixtures()

    assert negatives(configured(config, "fx_chest_pain")) == (
        (Negative.PREFIXED, "cchest pain"),
        (Negative.SUFFIXED, "chest painp"),
    )
    hindi = negatives(configured(config, "fx_chest_pain_hi"))
    assert [kind for kind, _ in hindi] == [Negative.PREFIXED, Negative.SUFFIXED]
    assert negatives(configured(config, "fx_chest_near")) == (
        (Negative.OUTSIDE_WINDOW, "chest x x x pain"),
    )


def test_the_filler_word_is_never_one_of_the_term_s_words(tmp_path: Path) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    edit_json(
        guard / "platform.json",
        lambda d: d["output"][1].update(sets=[["x", "xx"], ["tablet"]]),
    )
    near = configured(load_guard_dir(guard, corpus), "fx_take_tablets")

    assert (Positive.WINDOW_EDGE, "x xxx xxx tablet") in positives(near)


# ── run_selftest ─────────────────────────────────────────────────────────────────────────────


def test_the_fixture_folder_passes_with_a_matcher_that_knows_the_answers() -> None:
    config = load_fixtures()
    matcher = AnswerKeyMatcher(config)

    result = run_selftest(config, matcher)

    assert result.status is SelftestStatus.PASSED
    assert result.terms == len(config.terms) == 16
    assert result.positives > result.terms
    # Two derived negatives per phrase, one per near term, and each fixed negative per set.
    near = sum(1 for t in config.terms if t.term.kind == "near")
    fixed = len(config.negatives) * (len(config.businesses) + 1)
    assert result.negatives == 2 * (result.terms - near) + near + fixed
    assert matcher.calls == result.positives + result.negatives


def test_while_h1_is_a_stub_the_matcher_is_reported_unavailable() -> None:
    result = run_selftest(load_fixtures(), stub)

    assert result.status is SelftestStatus.MATCHER_UNAVAILABLE
    assert (result.positives, result.negatives) == (0, 0)


def test_a_miss_stops_startup_naming_the_location_and_variant_never_the_term() -> None:
    with pytest.raises(SelftestError) as error:
        run_selftest(load_fixtures(), never)

    assert str(error.value) == "G14: platform.json: red_flags.0 (itself) does not match its term"
    assert isinstance(error.value, GuardConfigError)


def test_a_hit_on_a_derived_negative_stops_startup() -> None:
    config = load_fixtures()
    answers = AnswerKeyMatcher(config)
    answers.answers["fx_chest_pain"] |= {screen_text("chest painp")}

    with pytest.raises(SelftestError) as error:
        run_selftest(config, answers)

    assert str(error.value) == (
        "G14: types/human_healthcare.json: red_flags.0 (suffixed negative) matches a term"
    )


def test_a_hit_on_a_fixed_negative_stops_startup() -> None:
    config = load_fixtures()
    answers = AnswerKeyMatcher(config)
    answers.answers["fx_rat_poison"] |= {screen_text("Is the painting class open on Sunday?")}

    with pytest.raises(SelftestError) as error:
        run_selftest(config, answers)

    # fx-clinic's set does not hold the veterinary term; fx-vet's does.
    assert str(error.value) == (
        "G14: platform.json: negatives.1 (configured negative, fx-vet's terms) matches a term"
    )


def test_an_always_hitting_matcher_fails_on_the_first_negative() -> None:
    with pytest.raises(SelftestError, match=r"\(prefixed negative\) matches a term"):
        run_selftest(load_fixtures(), always)


def test_any_other_matcher_error_stops_startup_naming_its_type_only() -> None:
    def broken(text: Folded, terms: TermSet) -> tuple[TermHit, ...]:
        raise RuntimeError(f"cannot match {text}")

    with pytest.raises(SelftestError) as error:
        run_selftest(load_fixtures(), broken)

    assert str(error.value) == (
        "G14: the term matcher raised RuntimeError on platform.json: red_flags.0 (itself)"
    )
    assert error.value.__context__ is None  # raised outside `except`, so no text rides along


def test_a_hit_for_a_term_the_matcher_was_not_given_stops_startup() -> None:
    def stray(text: Folded, terms: TermSet) -> tuple[TermHit, ...]:
        return (TermHit("fx_not_given", TermCategory.EMERGENCY, 0),)

    with pytest.raises(SelftestError) as error:
        run_selftest(load_fixtures(), stray)

    assert str(error.value) == (
        "G14: the term matcher returned a hit it was not given on platform.json: red_flags.0"
        " (itself)"
    )


# ── verified_hits ────────────────────────────────────────────────────────────────────────────

TERMS = TermSet((PhraseTerm("fx_a", TermCategory.EMERGENCY, ("a",)),))


def test_verified_hits_passes_hits_of_the_given_terms_through() -> None:
    hits = (TermHit("fx_a", TermCategory.EMERGENCY, 3),)

    assert verified_hits(hits, TERMS) == hits
    assert verified_hits((), TERMS) == ()


@pytest.mark.parametrize(
    "hit",
    [
        TermHit("fx_b", TermCategory.EMERGENCY, 0),  # a term it was not given
        TermHit("fx_a", TermCategory.PROFESSIONAL_JUDGEMENT, 0),  # another category
        ("fx_a", TermCategory.EMERGENCY, 0),  # not a TermHit
    ],
)
def test_verified_hits_refuses_what_the_matcher_was_not_given(hit: object) -> None:
    with pytest.raises(UnexpectedHit, match="a hit for a term it was not given"):
        verified_hits((hit,), TERMS)  # type: ignore[arg-type]

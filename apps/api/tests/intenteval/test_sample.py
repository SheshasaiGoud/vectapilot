"""M4 PR 1: D3's seeded, stratified verification sample of the training rows."""

import hashlib
from typing import Final

import pytest

from tests.intenteval.helpers import manifest, train_row
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.intenteval.rows import TrainRow, Verification
from vectapilot_api.intenteval.sample import (
    draw_sample,
    load_sample,
    render_sample,
    sample_problems,
)

VERIFICATION: Final = manifest().verification


def strata_rows() -> list[TrainRow]:
    """Three strata: 10 English questions, 3 English emergencies, 1 Hinglish emergency."""
    rows = [train_row(id=f"example-q{k}", intent="question") for k in range(10)]
    rows += [train_row(id=f"example-e{k}", intent="emergency") for k in range(3)]
    rows.append(train_row(id="example-h0", intent="emergency", language="hi", code_mixed=True))
    return rows


def test_each_stratum_gives_at_least_its_share_rounded_up() -> None:
    sample = draw_sample(strata_rows(), VERIFICATION)
    # ceil(10 * 20 %) = 2, ceil(3 * 20 %) = 1, ceil(1 * 20 %) = 1: no small cell is skipped.
    assert sum(i.startswith("example-q") for i in sample) == 2
    assert sum(i.startswith("example-e") for i in sample) == 1
    assert "example-h0" in sample
    assert len(sample) * 100 >= VERIFICATION.percent * len(strata_rows())
    assert list(sample) == sorted(sample)


def test_hindi_and_hinglish_are_separate_strata() -> None:
    # strata_rows() has no plain-Hindi emergency, so merging hi and hi-code-mixed changes
    # nothing there. Here one stratum of 5 would give ceil(5 * 20 %) = 1 row, and the Hinglish
    # one could go unverified.
    hindi = [train_row(id=f"example-hi{k}", intent="emergency", language="hi") for k in range(4)]
    hinglish = train_row(id="example-hm0", intent="emergency", language="hi", code_mixed=True)
    sample = draw_sample([*hindi, hinglish], VERIFICATION)
    # ceil(4 * 20 %) = 1 plain-Hindi row, ceil(1 * 20 %) = 1 Hinglish row.
    assert len(sample) == 2
    assert "example-hm0" in sample


def test_the_draw_is_seeded_and_ignores_file_order() -> None:
    rows = strata_rows()
    shuffled = sorted(rows, key=lambda r: hashlib.sha256(r.id.encode()).hexdigest())
    assert shuffled != rows
    assert draw_sample(shuffled, VERIFICATION) == draw_sample(rows, VERIFICATION)
    many = [train_row(id=f"example-r{k}") for k in range(40)]
    seeds = {draw_sample(many, Verification(percent=20, seed=f"s{k}")) for k in range(5)}
    assert len(seeds) > 1  # the seed, not the ids' order, picks the rows


def test_the_sample_file_round_trips_and_refuses_junk() -> None:
    assert load_sample(render_sample(("example-a", "example-b"))) == ("example-a", "example-b")
    assert load_sample("\nexample-a\n\n") == ("example-a",)
    with pytest.raises(DatasetError, match="line 2 is not a row id"):
        load_sample("example-a\nnot an id\n")
    with pytest.raises(DatasetError, match="lists an id more than once"):
        load_sample("example-a\nexample-a\n")


def test_a_complete_verified_sample_has_no_problems() -> None:
    rows = [r.model_copy(update={"verified": True}) for r in strata_rows()]
    assert sample_problems(rows, draw_sample(rows, VERIFICATION), VERIFICATION) == []


def test_what_the_freeze_needs_of_the_sample() -> None:
    rows = strata_rows()
    assert sample_problems(rows, None, VERIFICATION) == [
        "no verification-sample.txt: run `sample`, then verify every row it lists"
    ]
    problems = sample_problems(rows, ("example-q0", "example-gone"), VERIFICATION)
    assert problems == [
        "verification-sample.txt lists example-gone, which is not a training row",
        "1 of 2 sampled training rows are not verified yet",
        "verification-sample.txt lists 2 of 14 training rows, fewer than 20 %: run `sample` "
        "again and verify the new ids",
    ]

"""H2 scaffold: the record H2's metrics take, and how it is built (built by the team; these pass).

`RankedRelevance` is checked when built, `ranked_relevance` turns a question's ranked hits into it
with `relevance.matched_groups`, and questions without evidence are listed as not scored, never
dropped. The metrics themselves (`metrics.py`) are shesha's H2, tested in `test_metrics.py`.
Every record and chunk here is made up (`example-` ids, `TEST-ONLY` text); none is an eval
question.
"""

import dataclasses
from typing import Any, Final

import pytest

from tests.evalset.helpers import PAWS_CANCEL, SUNRISE_FEE, SUNRISE_HOURS, question
from vectapilot_api.evalset.questions import EvalQuestion
from vectapilot_api.evalset.ranking import (
    RankedRelevance,
    check_k,
    check_results,
    has_evidence,
    ranked_relevance,
    select_for_retrieval,
)
from vectapilot_api.evalset.relevance import RetrievedChunk

HOURS: Final = "Saturday: 9:00 to 14:00"  # SUNRISE_HOURS's anchor (faq 2026-10)
FEE: Final = "General consultation (general physician): INR 600"  # SUNRISE_FEE's (fees 2026-04)


def chunk(
    content: str,
    *,
    business_id: str = "sunrise-clinic",
    source_id: str = "faq",
    version: str = "2026-10",
) -> RetrievedChunk:
    return RetrievedChunk(business_id, source_id, version, f"TEST-ONLY before. {content} After.")


def fee_chunk(content: str = FEE) -> RetrievedChunk:
    return chunk(content, source_id="fees", version="2026-04")


def multi_hop() -> EvalQuestion:
    """Two groups: 0 = the Saturday hours (faq), 1 = the consultation fee (fees PDF)."""
    return question(
        id="example-multi",
        category="multi_hop",
        expected_evidence=[[SUNRISE_HOURS], [SUNRISE_FEE]],
    )


# --- RankedRelevance: what a valid record is -----------------------------------------------------


def test_a_valid_record_keeps_its_ranks_as_given() -> None:
    record = RankedRelevance("example-q", 2, ((), (1,), (0, 1), (0,)))

    assert record.question_id == "example-q"
    assert record.n_groups == 2
    # groups_by_rank[i] is rank i + 1: rank 1 finds nothing, rank 2 finds group 1, and so on.
    assert record.groups_by_rank[1] == (1,)


def test_an_empty_hit_list_is_a_valid_record() -> None:
    # A search that returned nothing is a miss for the metrics, not a reason to skip the question.
    assert RankedRelevance("example-q", 1, ()).groups_by_rank == ()


def test_the_same_group_may_be_found_at_several_ranks() -> None:
    # Two chunks can hold the same anchor, and interchangeable keys sit in different documents.
    assert RankedRelevance("example-q", 1, ((0,), (), (0,))).groups_by_rank == ((0,), (), (0,))


def test_a_record_without_evidence_groups_cannot_exist() -> None:
    with pytest.raises(ValueError, match="not scored for retrieval") as excinfo:
        RankedRelevance("example-no-evidence", 0, ())
    assert "example-no-evidence" in str(excinfo.value)


@pytest.mark.parametrize(
    ("n_groups", "groups_by_rank", "rule"),
    [
        (-1, (), "at least 1"),
        (True, ((0,),), "n_groups must be an int"),  # True == 1 in Python, still refused
        (2, ((2,),), "outside 0..1"),  # indexes start at 0: 2 groups are 0 and 1
        (2, ((-1,),), "outside 0..1"),
        (2, ((True,),), "group index must be an int"),
        (2, ((1, 0),), "ascending"),  # matched_groups lists them ascending
        (2, ((0, 0),), "each listed once"),
        (1, [(0,)], "groups_by_rank must be a tuple"),
        (1, ([0],), "the groups found must be a tuple"),
    ],
)
def test_a_broken_record_is_refused_with_its_rule(
    n_groups: Any, groups_by_rank: Any, rule: str
) -> None:
    with pytest.raises(ValueError, match=rule) as excinfo:
        RankedRelevance("example-broken", n_groups, groups_by_rank)
    assert excinfo.type is ValueError
    assert "example-broken" in str(excinfo.value)  # the message names the question


@pytest.mark.parametrize("question_id", ["", 7])
def test_a_record_needs_a_question_id(question_id: Any) -> None:
    with pytest.raises(ValueError, match="question_id"):
        RankedRelevance(question_id, 1, ())


def test_a_record_is_frozen() -> None:
    record = RankedRelevance("example-q", 1, ((0,),))
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.n_groups = 2  # type: ignore[misc]


# --- ranked_relevance: from matched_groups to the record -----------------------------------------


def test_each_hit_becomes_the_groups_it_finds_in_search_order() -> None:
    hits = [
        chunk("nothing relevant"),  # rank 1
        fee_chunk(),  # rank 2: group 1
        chunk(HOURS),  # rank 3: group 0
        fee_chunk(f"{HOURS}. {FEE}"),  # rank 4: the fees PDF does not hold group 0's key
        chunk(HOURS),  # rank 5: group 0 again
    ]

    record = ranked_relevance(multi_hop(), hits)

    assert record == RankedRelevance("example-multi", 2, ((), (1,), (0,), (1,), (0,)))


def test_one_hit_can_find_two_groups() -> None:
    # Group 0 accepts a quote from the fees PDF too (interchangeable keys), so one fees chunk can
    # hold both groups' anchors.
    row = question(
        id="example-multi",
        category="multi_hop",
        expected_evidence=[
            [SUNRISE_HOURS, {**SUNRISE_FEE, "anchor": "TEST-ONLY hours line"}],
            [SUNRISE_FEE],
        ],
    )

    record = ranked_relevance(row, [fee_chunk(f"TEST-ONLY hours line. {FEE}")])

    assert record.groups_by_rank == ((0, 1),)


def test_another_businesss_hit_finds_nothing() -> None:
    record = ranked_relevance(multi_hop(), [chunk(HOURS, business_id="paws-and-co")])
    assert record.groups_by_rank == ((),)


def test_no_hits_give_an_empty_record_not_an_error() -> None:
    assert ranked_relevance(multi_hop(), []) == RankedRelevance("example-multi", 2, ())


def test_the_hits_are_not_modified() -> None:
    hits = [chunk(HOURS), fee_chunk()]
    before = list(hits)

    ranked_relevance(multi_hop(), hits)

    assert hits == before


def test_a_question_without_evidence_is_refused_by_name() -> None:
    row = question(
        id="example-dont-know",
        category="out_of_scope",
        expected_outcome="dont_know",
        expected_evidence=[],
    )
    with pytest.raises(ValueError, match="not scored for retrieval") as excinfo:
        ranked_relevance(row, [chunk(HOURS)])
    assert "example-dont-know" in str(excinfo.value)


# --- select_for_retrieval: the explicit exclusion ------------------------------------------------


def test_questions_without_evidence_are_listed_as_not_scored_never_dropped() -> None:
    answered = question(id="example-faq")
    dont_know = question(
        id="example-oos",
        category="out_of_scope",
        expected_outcome="dont_know",
        expected_evidence=[],
    )
    handoff_without = question(
        id="example-handoff",
        category="handoff_request",
        expected_outcome="handoff",
        expected_evidence=[],
    )
    # A hand-off may point at evidence (what the reply refers to), and then it is scored.
    handoff_with = question(
        id="example-safety",
        category="safety",
        business_id="paws-and-co",
        expected_outcome="handoff",
        expected_evidence=[[PAWS_CANCEL]],
    )
    questions = [dont_know, answered, handoff_with, handoff_without, multi_hop()]

    selection = select_for_retrieval(questions)

    assert [q.id for q in selection.scored] == ["example-faq", "example-safety", "example-multi"]
    assert [q.id for q in selection.not_scored] == ["example-oos", "example-handoff"]
    assert len(selection.scored) + len(selection.not_scored) == len(questions)
    assert [has_evidence(q) for q in questions] == [False, True, True, False, True]


def test_selecting_from_no_questions_gives_two_empty_parts() -> None:
    selection = select_for_retrieval([])
    assert (selection.scored, selection.not_scored) == ((), ())


# --- check_k and check_results -------------------------------------------------------------------


@pytest.mark.parametrize("k", [1, 5, 50, 1000])
def test_any_int_of_at_least_one_is_a_valid_k(k: int) -> None:
    check_k(k)  # no error: a k beyond the list is the metric's business, not an error


@pytest.mark.parametrize(
    ("k", "rule"),
    [
        (0, "at least 1"),
        (-1, "at least 1"),
        (True, "must be an int"),  # True == 1 in Python, still refused
        (False, "must be an int"),
        (5.0, "must be an int"),
        ("5", "must be an int"),
    ],
)
def test_a_bad_k_is_refused(k: Any, rule: str) -> None:
    with pytest.raises(ValueError, match=rule) as excinfo:
        check_k(k)
    assert excinfo.type is ValueError


def test_a_set_of_distinct_questions_passes_check_results() -> None:
    check_results([RankedRelevance("example-a", 1, ()), RankedRelevance("example-b", 1, ((0,),))])
    check_results((RankedRelevance("example-a", 1, ()),))  # any sequence


def test_an_empty_set_is_refused() -> None:
    with pytest.raises(ValueError, match="empty set") as excinfo:
        check_results([])
    assert excinfo.type is ValueError


SAME: Final = RankedRelevance("example-twice", 1, ((0,),))


@pytest.mark.parametrize(
    "repeat",
    [
        SAME,  # the same record again
        # A different record with the same id, as when two runs are merged: a check by record
        # equality would miss it, so the question would weigh double.
        RankedRelevance("example-twice", 1, ()),
    ],
)
def test_a_question_listed_twice_is_refused_by_name(repeat: RankedRelevance) -> None:
    with pytest.raises(ValueError, match="example-twice is listed more than once"):
        check_results([SAME, RankedRelevance("example-other", 1, ()), repeat])

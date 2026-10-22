"""M2 PR 7: when a retrieved chunk is evidence for a question (hand-built, TEST-ONLY chunks).

These pin the matching rule PR 8 scores with. The metrics on top (hit@k, MRR) are shesha's H2
and are not tested, or written, here.
"""

from typing import Final

import pytest

from tests.evalset.helpers import PAWS_CANCEL, SUNRISE_FEE, SUNRISE_HOURS, question
from vectapilot_api.evalset.questions import SourceKey
from vectapilot_api.evalset.relevance import RetrievedChunk, hit_matches_key, matched_groups

KEY: Final = SourceKey.model_validate(SUNRISE_HOURS)  # faq 2026-10, "Saturday: 9:00 to 14:00"


def chunk(
    content: str,
    *,
    business_id: str = "sunrise-clinic",
    source_id: str = "faq",
    version: str = "2026-10",
) -> RetrievedChunk:
    return RetrievedChunk(business_id, source_id, version, f"TEST-ONLY before. {content} After.")


def test_a_hit_from_the_keys_document_that_holds_the_anchor_matches() -> None:
    assert hit_matches_key(chunk("Saturday: 9:00 to 14:00"), "sunrise-clinic", KEY)
    # Whitespace in the chunk is normalised like an anchor: a line break matches a space.
    assert hit_matches_key(chunk("Saturday:\n9:00  to 14:00"), "sunrise-clinic", KEY)


def test_an_nfd_chunk_matches_an_nfc_anchor() -> None:
    key = SourceKey(source_id="faq", version="2026-10", anchor="Café hours apply")
    decomposed = "Café hours apply"  # e + combining acute accent
    assert hit_matches_key(chunk(decomposed), "sunrise-clinic", key)


@pytest.mark.parametrize(
    "hit",
    [
        # Another business's chunk never counts, even with the same source id and text: a leak.
        chunk("Saturday: 9:00 to 14:00", business_id="paws-and-co"),
        chunk("Saturday: 9:00 to 14:00", source_id="FAQ"),  # case-only source_id
        chunk("Saturday: 9:00 to 14:00", source_id="faq-hi"),
        chunk("Saturday: 9:00 to 14:00", version="2025-10"),  # another version
        chunk("saturday: 9:00 to 14:00"),  # case-only anchor
        chunk("Saturday: 9:00 to"),  # only part of the anchor (it straddles chunks)
    ],
)
def test_anything_else_does_not_match(hit: RetrievedChunk) -> None:
    assert not hit_matches_key(hit, "sunrise-clinic", KEY)


def test_matched_groups_lists_every_group_a_hit_finds() -> None:
    # Group 0: the hours, from faq or (interchangeably) the fees PDF's line; group 1: the fee.
    row = question(
        category="multi_hop",
        expected_evidence=[
            [SUNRISE_HOURS, {**SUNRISE_FEE, "anchor": "TEST-ONLY hours line"}],
            [SUNRISE_FEE],
        ],
    )
    assert matched_groups(chunk("Saturday: 9:00 to 14:00"), row) == (0,)
    pdf = chunk("TEST-ONLY hours line", source_id="fees", version="2026-04")
    assert matched_groups(pdf, row) == (0,)  # either key of group 0 finds it
    both = chunk(
        "TEST-ONLY hours line. General consultation (general physician): INR 600",
        source_id="fees",
        version="2026-04",
    )
    assert matched_groups(both, row) == (0, 1)
    assert matched_groups(chunk("nothing relevant"), row) == ()


def test_a_question_without_evidence_has_no_groups_to_find() -> None:
    row = question(category="out_of_scope", expected_outcome="dont_know", expected_evidence=[])
    assert matched_groups(chunk("Saturday: 9:00 to 14:00"), row) == ()


def test_the_question_s_business_decides_not_the_hit_s() -> None:
    paws = question(business_id="paws-and-co", expected_evidence=[[PAWS_CANCEL]])
    hit = RetrievedChunk(
        "sunrise-clinic",
        "appointment-policy",
        "2026-09",
        "TEST-ONLY free up to 12 hours before it starts",
    )
    assert matched_groups(hit, paws) == ()
    assert matched_groups(
        RetrievedChunk("paws-and-co", *(hit.source_id, hit.version, hit.content)), paws
    ) == (0,)


def test_a_chunk_keeps_its_content_out_of_repr() -> None:
    assert "TEST-ONLY" not in repr(chunk("Saturday: 9:00 to 14:00"))

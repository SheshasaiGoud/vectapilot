"""When a retrieved chunk counts as evidence for a question (M2 PR 7; the rule PR 8 scores with).

By content, not by chunk number (ananya's review, F2; decided 2026-10-22, item 3). A hit matches
an evidence key when

1. it belongs to the question's business. PR 8 fills `RetrievedChunk.business_id` from the hit's
   document row, never from the question, so a hit from another business fails here (and PR 8
   also reports it as a leak);
2. its `(source_id, version)` equals the key's, exactly (case included); and
3. its content, normalised like an anchor (NFC, whitespace runs collapsed), contains the key's
   anchor, exactly (case included).

That holds at any chunk size and survives reindexing. A hit finds an evidence group when it
matches any key of the group (the keys of a group are interchangeable).

`matched_groups` turns one ranked hit into the groups it finds; PR 8 turns a question's ranked
hits into a ranked list of those. What hit@k and MRR make of that list is shesha's H2
(`[HAND-WRITE]`), and is not here.
"""

from dataclasses import dataclass, field

from vectapilot_api.evalset.corpus import normalize_anchor
from vectapilot_api.evalset.questions import EvalQuestion, SourceKey


@dataclass(frozen=True)
class RetrievedChunk:
    """What relevance needs of one retrieved chunk (PR 8 builds it from a `SearchHit` and the
    business of the hit's document)."""

    business_id: str
    source_id: str
    version: str
    content: str = field(repr=False)


def hit_matches_key(hit: RetrievedChunk, business_id: str, key: SourceKey) -> bool:
    """Whether `hit` is the evidence `key` names for a question of `business_id` (the module
    docstring's three conditions)."""
    return (
        hit.business_id == business_id
        and (hit.source_id, hit.version) == (key.source_id, key.version)
        and key.anchor in normalize_anchor(hit.content)
    )


def matched_groups(hit: RetrievedChunk, question: EvalQuestion) -> tuple[int, ...]:
    """The indexes (from 0, ascending) of the question's evidence groups that `hit` finds. Empty
    for a hit that finds none, and for a question without evidence."""
    return tuple(
        index
        for index, group in enumerate(question.expected_evidence)
        if any(hit_matches_key(hit, question.business_id, key) for key in group)
    )

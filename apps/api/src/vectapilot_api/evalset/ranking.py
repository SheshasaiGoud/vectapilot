"""What H2's retrieval metrics take: one question's ranked relevance (M2, H2 scaffold; built and
tested by the team, not `[HAND-WRITE]`).

`relevance.matched_groups` says, for one retrieved hit, which of a question's evidence groups it
finds. `ranked_relevance` applies it to a question's hits in the order the search returned them,
and `RankedRelevance` is the result: per rank, the groups found, plus the number of groups. That
record is the whole input of hit@k and MRR (`metrics.py`, shesha's H2), so it carries no text,
score, chunk id or business: only what the metrics need.

**Questions without evidence are excluded explicitly, never silently.** A question with no
evidence group (`dont_know`, `out_of_scope`, a `handoff` that points to nothing) has nothing to
find, so hit@k and MRR are undefined for it, not 0. `RankedRelevance` refuses `n_groups` 0, so no
record for such a question can exist; `select_for_retrieval` splits a question set into the scored
and the not-scored ones, so a report can say "n scored, m not scored (no evidence)" and both add up
to the questions it started from. Every other question counts, an empty hit list included: a
search that returned nothing is a miss, never a skip. One exception is PR 8's, not checked here:
before calling `ranked_relevance`, the harness takes out any question with an evidence group whose
every key no stored chunk holds at this config, and reports it as "unscorable at this config", a
third count next to scored and not scored (a chunking artefact is not a retrieval miss).

Also here, because every metric needs them: `check_k` (the cut-off is an int >= 1) and
`check_results` (a question set is non-empty and names each question once).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from vectapilot_api.evalset.questions import EvalQuestion
from vectapilot_api.evalset.relevance import RetrievedChunk, matched_groups


@dataclass(frozen=True)
class RankedRelevance:
    """One question's retrieval result, as the metrics see it.

    - `question_id`: the question's id (non-empty). Reports and errors name it; the metrics use it
      only to refuse a question counted twice (`check_results`).
    - `n_groups`: how many evidence groups the question has, at least 1.
    - `groups_by_rank`: one entry per retrieved hit, best first. **`groups_by_rank[i]` holds the
      groups found by the hit at rank `i + 1`**: Python positions start at 0, ranks start at 1.
      Each entry is a tuple of group indexes (`0 <= index < n_groups`, ascending, each once), as
      `relevance.matched_groups` returns them; `()` means that hit is not relevant. The same group
      may appear at several ranks (two chunks can hold the same anchor, and interchangeable keys
      sit in different documents). An empty `groups_by_rank` is a search that returned no hits.

    Frozen, and checked when built: a broken record raises `ValueError` naming the question and
    the rule. Tuples only (a list inside a frozen record could still change).
    """

    question_id: str
    n_groups: int
    groups_by_rank: tuple[tuple[int, ...], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.question_id, str) or not self.question_id:
            raise ValueError("question_id must be a non-empty string")
        where = f"question {self.question_id}"
        if isinstance(self.n_groups, bool) or not isinstance(self.n_groups, int):
            raise ValueError(f"{where}: n_groups must be an int")
        if self.n_groups < 1:
            raise ValueError(
                f"{where}: n_groups must be at least 1; a question without evidence groups is "
                "not scored for retrieval (select_for_retrieval lists it as not scored)"
            )
        if not isinstance(self.groups_by_rank, tuple):
            raise ValueError(f"{where}: groups_by_rank must be a tuple")
        for position, groups in enumerate(self.groups_by_rank):
            rank = f"{where}, rank {position + 1}"
            if not isinstance(groups, tuple):
                raise ValueError(f"{rank}: the groups found must be a tuple")
            for index in groups:
                if isinstance(index, bool) or not isinstance(index, int):
                    raise ValueError(f"{rank}: a group index must be an int")
                if not 0 <= index < self.n_groups:
                    raise ValueError(
                        f"{rank}: group index {index} is outside 0..{self.n_groups - 1}"
                    )
            if any(a >= b for a, b in pairwise(groups)):
                raise ValueError(f"{rank}: group indexes must be ascending, each listed once")


def has_evidence(question: EvalQuestion) -> bool:
    """Whether hit@k and MRR are defined for `question`: it has at least one evidence group."""
    return len(question.expected_evidence) > 0


@dataclass(frozen=True)
class RetrievalSelection:
    """A question set split for retrieval scoring, both parts in their input order.

    `scored` have at least one evidence group; `not_scored` have none and are reported as such
    (by count, and by category if the report breaks it down), never dropped without a word.
    """

    scored: tuple[EvalQuestion, ...]
    not_scored: tuple[EvalQuestion, ...]


def select_for_retrieval(questions: Sequence[EvalQuestion]) -> RetrievalSelection:
    """Split `questions` into those hit@k and MRR are defined for and those they are not
    (`has_evidence`). Nothing is dropped: the two parts together hold every question once. An
    empty input gives two empty parts; the metrics refuse an empty set themselves."""
    return RetrievalSelection(
        scored=tuple(q for q in questions if has_evidence(q)),
        not_scored=tuple(q for q in questions if not has_evidence(q)),
    )


def ranked_relevance(question: EvalQuestion, hits: Sequence[RetrievedChunk]) -> RankedRelevance:
    """The record H2's metrics take for `question`, from its `hits` in the order the search
    returned them (best first; the search's tie-break already made that order total).

    `hits` is the search's ranked list **before the score threshold**: `AnswerRun.hits` or
    `SearchResult.hits`, never `AnswerRun.evidence` (the hits at or above `rag_min_score`). So
    hit@k and MRR@k measure retrieval alone, and tuning the threshold cannot move them.

    One entry per hit: `relevance.matched_groups(hit, question)`. So a hit from another business
    finds nothing here (PR 8 reports it as a leak as well). `hits` is not modified and may be
    empty. Raises `ValueError` naming the question when it has no evidence group: such a question
    is not scored for retrieval (use `select_for_retrieval` first). It does not check whether
    every group is findable at this config: PR 8 takes unscorable questions out first (module
    docstring).
    """
    if not has_evidence(question):
        raise ValueError(
            f"question {question.id} has no evidence group, so it is not scored for retrieval "
            "(select_for_retrieval lists it as not scored)"
        )
    return RankedRelevance(
        question_id=question.id,
        n_groups=len(question.expected_evidence),
        groups_by_rank=tuple(matched_groups(hit, question) for hit in hits),
    )


def check_k(k: int) -> None:
    """Raise `ValueError` unless `k` is an int of at least 1. A bool is refused even though Python
    treats `True` as 1: `hit_at_k(result, True)` is a mistake, not a cut-off."""
    if isinstance(k, bool) or not isinstance(k, int):
        raise ValueError("k must be an int")
    if k < 1:
        raise ValueError("k must be at least 1 (ranks start at 1)")


def check_results(results: Sequence[RankedRelevance]) -> None:
    """Raise `ValueError` when `results` is empty (a mean over no questions is undefined, not 0)
    or names a question twice (it would weigh double in a mean). The message names the repeated
    question."""
    if len(results) == 0:
        raise ValueError("no questions to score: a mean over an empty set is undefined")
    seen: set[str] = set()
    for result in results:
        if result.question_id in seen:
            raise ValueError(f"question {result.question_id} is listed more than once")
        seen.add(result.question_id)

"""H2 (M3) — Reciprocal Rank Fusion of ranked hit lists. [HAND-WRITE], written by shesha (D5);
time-box 2 h.

Guide: docs/learning/m3-hybrid.md · Tests: tests/retrieval/test_fusion.py (no database), strict
xfails until the body is written. ADR 0014 decision 4 (D6 (a)).
"""

from collections.abc import Sequence
from typing import Final

from vectapilot_api.retrieval.types import FusedHit, SearchHit

RRF_K: Final = 60
"""RRF's constant k (Cormack, Clarke and Büttcher, 2009), fixed, not tuned: about 28 dev questions
would fit noise (ADR 0014 decision 4). PR 4's `rag_rrf_k` setting defaults to it."""


def rrf_fuse(
    ranked_lists: Sequence[Sequence[SearchHit]], *, k: int = RRF_K, limit: int
) -> list[FusedHit]:
    """Fuse `ranked_lists` (each best first) into one list, best first, by Reciprocal Rank Fusion.

    Contract:

    - **Input.** Each list is ranked best first, as a search leg returned it; ranks count from 1
      in each list. PR 4 passes `[vector_search's hits, text_search's hits]`, both for one
      business. The lists and the hits are never changed.
    - **Score.** For each distinct chunk (by `chunk_id`), `rrf_score` = Σ 1/(k + rank) over the
      lists that hold it, as an exact `fractions.Fraction`: never a float, not even on the way.
      Two sums that are equal as fractions must tie exactly, and as floats they can differ in
      the last bit: each 1/(k + rank) is rounded before any sum starts, so no way of adding
      floats (a running total, `sum()`, `math.fsum`) is safe. Only ranks count: a hit's own
      `score` (its cosine) enters neither the fused score nor the order.
    - **Ranks.** Each `FusedHit.ranks` has one entry per input list, in the order of
      `ranked_lists`: the hit's rank there, or None when that list does not hold it.
    - **Hit.** `FusedHit.hit` is the `SearchHit` as given, unchanged; its `score` stays the cosine
      for ADR 0011's evidence gate (D7).
    - **Order.** `rrf_score`, highest first. Ties are broken by the best (lowest) single rank, then
      in M2 H1's order: `source_id`, then `version` (both compared code point by code point, as
      Python compares `str`, the same order as Postgres' `COLLATE "C"` on UTF-8 text), then
      `chunk_index`, all ascending. Within one business those three name one chunk, so the order
      is total. It never depends on the order of `ranked_lists`, on dict or set iteration, or on
      the hash seed (`PYTHONHASHSEED`).
    - **How many.** At most `limit` fused hits, the best ones; fewer when there are fewer distinct
      chunks.
    - **Duplicates raise `ValueError`.** A chunk id twice in one list, even as two equal hits. The
      same chunk id in two lists with hits that are not equal (`!=`): both legs build the same
      `SearchHit` for one chunk (same fields, same cosine), so a difference means something mixed
      models, versions or businesses. Hits are compared with `==`, never `is`: each leg builds
      its own object, so one chunk found by both legs arrives as two equal but separate hits.
      The message never contains a hit's content or title.
    - **Empty.** No lists, or only empty lists, give `[]`.
    - **Arguments.** `k` and `limit` are at least 1, else `ValueError` (decided 2026-10-24).
    - **Pure.** No I/O, no logging, no randomness, no clock.

    Not this function's job: running the legs and the transaction (PR 4), the evidence gate
    (`hit.score` against `rag_min_score`, D7), counting FTS-only hits (PR 4 reads `ranks`), and
    reranking (not in M3, D13).
    """
    raise NotImplementedError("H2 (M3): write rrf_fuse (guide: docs/learning/m3-hybrid.md)")

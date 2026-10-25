"""Retrieval (M2 H1, M3): find a business's chunks for a question.

- `types.py` — `VectorQuery` and `TextQuery` (the question as a vector, and as text too, validated
  before any SQL), `Question`, `SearchHit` (one retrieved chunk with what a citation needs) and
  `FusedHit` (one hit of a fused list). Built and tested.
- `vector.py` — `vector_search`, top-k exact vector search. **[HAND-WRITE]** (M2 H1), written by
  Claude at shesha's request (guide: docs/learning/m2-retrieval.md).
- `text.py` — `text_search`, top-k Postgres full-text search. **[HAND-WRITE]** (M3 H1), a stub
  until written (guide: docs/learning/m3-hybrid.md).
- `fusion.py` — `rrf_fuse`, Reciprocal Rank Fusion. **[HAND-WRITE]** (M3 H2), written by shesha,
  a stub until then (same guide).
- `search.py` — the search use case (M2 PR 6, M3 PR 4): embed the question, refuse mixed
  models, then `vector_search` in one business transaction (vector mode, M2's path), or both
  legs and `rrf_fuse` in one read-only snapshot (hybrid mode, `fused_search`). Depends only on
  the three functions' interfaces.
- `router.py` — `GET /v1/kb/search` (M2 PR 6), in the configured mode.

Reranking is not built in M3 (D13).
"""

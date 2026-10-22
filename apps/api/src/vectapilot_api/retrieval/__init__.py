"""Retrieval (M2 H1, M3): find a business's chunks for a question.

- `types.py` — `VectorQuery` (the question as a vector, validated before any SQL) and `SearchHit`
  (one retrieved chunk with what a citation needs). Built and tested.
- `vector.py` — `vector_search`, top-k exact vector search. **[HAND-WRITE]**: shesha writes its
  body (guide: docs/learning/m2-retrieval.md).

Full-text search, hybrid fusion (RRF) and reranking join this package in M3.
"""

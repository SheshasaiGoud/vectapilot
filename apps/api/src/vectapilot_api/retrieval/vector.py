"""H1 — top-k exact vector search for one business. [HAND-WRITE]: shesha writes the body.

Guide: docs/learning/m2-retrieval.md · Tests: tests/retrieval/test_vector_search.py (real Postgres,
CI) and tests/retrieval/test_vector_search_binding.py (no database), both marked
`xfail(strict=True)` until this works; then delete their `pytestmark` xfail. The canary suite runs
it too (`tests/canary/read_paths.py`: then also remove "vector_search" from `PENDING_HAND_WRITE`).
"""

from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.retrieval.types import SearchHit, VectorQuery


async def vector_search(connection: AsyncConnection, query: VectorQuery) -> list[SearchHit]:
    """Return the `query.k` chunks of `query.business_id` closest to `query.embedding`, best first.

    Contract:

    - **Where it runs.** Inside the caller's open business transaction: `connection` comes from
      `Database.business_transaction(query.business_id)` (the API, PR 6) or
      `db.business_transaction(engine, query.business_id)` (tests, the canary suite). This function
      runs exactly one statement, a `SELECT`, through `connection.execute`; it never begins,
      commits or rolls back, and never calls `set_config`.
    - **Business.** The statement's `WHERE` clause itself requires `chunks.business_id` to equal
      `query.business_id`, on top of row-level security: the filter is part of the search, never
      applied afterwards (business-context §5). So the result is the same on a connection where RLS
      does not apply (a superuser owner), and empty when the transaction was opened for another
      business (RLS and the filter then disagree, and both must hold).
    - **Candidates.** A chunk qualifies only if all of these hold:
      its `embedding_model` equals `query.embedding_model` (ADR 0008: two models' vectors are never
      compared); its document's `status` is `'approved'` (`draft` and `retired` never are,
      business-context §3.4); its document's `audience` is one of `query.audiences` (a customer gets
      `public` only, staff get `public` and `staff_only`). Join each chunk to its document on both
      keys, `business_id` and the document id, like the composite foreign key.
    - **Versions.** A source has at most one approved version at a time (decided 2026-10-22,
      the guide's §5): migration 0004 (PR 5b, diego) enforces it with a partial unique index that
      starts with `business_id`, and approving a new version retires the previous one in the same
      transaction. So the status filter above already keeps superseded (retired) and next (draft)
      versions out: this statement neither filters nor picks versions.
    - **Distance and score.** Exact search (no index, ADR 0008) with pgvector's cosine distance,
      the `<=>` operator. `score` = 1 - cosine distance, the cosine similarity, in [-1, 1]; higher
      is closer. For unit vectors it equals the dot product. No score threshold here: deciding
      that the evidence is too weak is the caller's job (PR 6).
    - **Order.** By score, highest first (cosine distance ascending). Ties are broken by the
      document's `source_id`, then its `version`, both compared byte by byte (`COLLATE "C"`), then
      `chunk_index`, all ascending: a total order on values that survive re-ingestion, so every run
      returns the same list (evals must be reproducible). Never by `chunks.id` (random) or by
      insertion order. With one approved version per source the version never decides between
      two candidates; it keeps the order total even on data that breaks that rule (a database
      without migration 0004).
    - **How many.** At most `query.k` hits (1 to `MAX_K`, already checked by `VectorQuery`); fewer
      when fewer chunks qualify, and `[]` when none does (not an error).
    - **Binding.** Every value is a bound parameter (`:name` in a `text()` statement and a
      parameters dict), never formatted into the SQL string: the business id, the model id, the
      audiences, `k`, and the vector, sent as pgvector's text form `kb.store.vector_literal(...)`
      and cast to `vector` inside the SQL (as `kb/store.py` writes chunks).
    - **Result.** One `SearchHit` per row, in order: the chunk's `id`, `document_id`, `page`,
      `chunk_index`, `content` and `embedding_model`; the document's `source_id`, `version`,
      `title`, `language` and `content_kind` (for PR 6's citations: returned, never filtered on);
      and the score.

    Not this function's job: refusing a business whose chunks carry several model ids (PR 6 calls
    `embeddings.guard.require_one_model` first; here other models' chunks are simply not
    candidates), embedding the question, the score threshold and the answer (PR 6), full-text
    search and fusion (M3), and filtering by language: there is no language filter in H1 (Q6,
    cross-lingual and code-mixed questions, is open; see the guide, §5).
    """
    # TODO(shesha, H1): one SELECT over chunks joined to their document (both keys);
    # TODO: WHERE business, model, approved, audience; the score; ORDER BY with the tie-break;
    # TODO: LIMIT k; every value bound; then each row becomes a SearchHit.
    raise NotImplementedError("H1: shesha writes vector_search, see docs/learning/m2-retrieval.md")

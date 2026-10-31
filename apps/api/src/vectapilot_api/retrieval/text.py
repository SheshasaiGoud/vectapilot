"""H1 (M3) — top-k full-text search for one business. [HAND-WRITE], written by Claude at
shesha's request (2026-10-28, for the deadline); shesha's explain-back is part of acceptance.

Guide: docs/learning/m3-hybrid.md · Tests: tests/retrieval/test_text_search.py (real Postgres,
CI and `make test-integration`) and tests/retrieval/test_text_search_binding.py (no database),
strict xfails until the body is written. ADR 0014 decision 3 (D6 (a)).
"""

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.kb.store import vector_literal
from vectapilot_api.retrieval.types import SearchHit, TextQuery

# One statement, every value bound. The question is parsed in SQL with each chunk's own config by
# `plainto_tsquery`, which with the platform's configs only emits quoted lexemes joined by `&`;
# turning those `&` into `|` gives the OR of the question's lexemes without ever reading the
# question as tsquery syntax.
# The select list is exactly SearchHit's fields; the score is the cosine, as in `vector_search`.
_SEARCH: Final = text(
    """
    SELECT c.id AS chunk_id, c.document_id, d.source_id, d.version, d.title, d.language,
           d.content_kind, c.page, c.chunk_index, c.content, c.embedding_model,
           1 - (c.embedding <=> CAST(:embedding AS vector)) AS score
    FROM chunks AS c
    JOIN documents AS d ON d.business_id = c.business_id AND d.id = c.document_id
    CROSS JOIN LATERAL (
        SELECT replace(plainto_tsquery(c.ts_config, :question)::text, ' & ', ' | ')::tsquery AS q
    ) AS question
    WHERE c.business_id = :business_id
      AND c.embedding_model = :embedding_model
      AND d.status = 'approved'
      AND d.audience = ANY(:audiences)
      AND c.tsv @@ question.q
    ORDER BY ts_rank(c.tsv, question.q) DESC,
             d.source_id COLLATE "C", d.version COLLATE "C", c.chunk_index
    LIMIT :k
    """
)


async def text_search(connection: AsyncConnection, query: TextQuery) -> list[SearchHit]:
    """Return the `query.k` chunks of `query.business_id` that best match `query.question` by
    Postgres full-text search, best first, each scored with its cosine to `query.embedding`.

    Contract:

    - **Where it runs.** Inside the caller's open business transaction, exactly like M2's
      `vector_search`: `connection` comes from `Database.business_transaction(query.business_id)`
      (the API, PR 4) or `db.business_transaction(engine, query.business_id)` (tests, the canary
      suite). This function runs exactly one statement, a `SELECT`, through
      `connection.execute`; it never begins, commits or rolls back, and never calls `set_config`.
    - **Business.** The statement's `WHERE` clause itself requires `c.business_id =
      :business_id`, on top of row-level security (business-context §5). So the result is the
      same on a connection where RLS does not apply (a superuser owner), and empty when the
      transaction was opened for another business (RLS and the filter then disagree, and both
      must hold).
    - **Candidates.** A chunk qualifies only if all of these hold, as in M2's `vector_search`:
      its `embedding_model` equals `query.embedding_model` (its cosine to the question is then
      defined, ADR 0008, and both legs of a hybrid search see the same candidates); its
      document's `status` is `'approved'` (one approved version per source, migration 0004, so
      this is also the version filter); its document's `audience` is one of `query.audiences`.
      Join each chunk to its document on both keys, `business_id` and the document id. All of
      these apply before the `LIMIT`.
    - **Matching** (ADR 0014 decision 3; D2, D6). A chunk qualifies only if its stored `c.tsv`
      matches an **OR** of the question's lexemes: it holds at least one of them.
      - The question is parsed **in SQL, with that chunk's own config, `c.ts_config`** (migration
        0005: `english`, `hindi` or `simple`, from the document's language). Never one config for
        every chunk, and never in Python: a lexeme only ever meets lexemes made by the same
        config. So an `english` chunk that says "opens" matches "opening" (both `open`), and a
        `simple` chunk matches the question's stop words, which `simple` keeps (a known effect,
        ADR 0014 "Consequences"; the tests plant one such chunk).
      - The question is untrusted text. It is bound unchanged, and never handed to `to_tsquery`
        (or cast to `tsquery`) as written: that syntax reads `& | ! ( ) :* <-> '` and `\\` as
        operators, so a customer's text would change the query or make Postgres raise, and the
        error quotes the text. Every question `TextQuery` accepts (up to `MAX_QUESTION_CHARS`
        characters of anything) returns normally, never raises, and is never logged.
      - Only lexemes count: no prefix matching (`:*`), no phrase or position operators, no
        weights. A question without a lexeme in a chunk's config (only stop words or
        punctuation) matches no chunk of that config; one without any lexeme at all gives `[]`.
    - **Rank.** `ts_rank(c.tsv, <that chunk's OR query>)` in its two-argument form (default
      weights, no length normalisation), highest first. It reads only this chunk's `tsv` and the
      question: no statistics over a corpus, so nothing another business stores can move this
      business's ranking (an isolation property, ADR 0014 decision 3).
    - **Order.** By rank, highest first. Ties in M2 H1's order: the document's `source_id`, then
      its `version`, both byte by byte (`COLLATE "C"`), then `chunk_index`, all ascending. Never
      by `chunks.id` (random), insertion order or the cosine.
    - **Score.** `score` is the cosine similarity to `query.embedding`, computed exactly as
      `vector_search` does, `1 - (c.embedding <=> CAST(:embedding AS vector))`. It never decides
      membership or order here. Why: ADR 0011's evidence gate compares `score` with
      `rag_min_score`, a cosine threshold (D7), and one chunk found by both legs must come back as
      two equal `SearchHit`s (`rrf_fuse` refuses two different hits with one chunk id).
    - **How many.** At most `query.k` hits (`LIMIT :k`); fewer when fewer chunks qualify, and `[]`
      when none does (not an error).
    - **Binding.** Every value is a bound parameter (`:name` in a `text()` statement and a
      parameters dict), never formatted into the SQL string: the business id, the model id, the
      audiences (a list), `k`, the vector as `kb.store.vector_literal(query.embedding)` cast to
      `vector` inside the SQL, and `query.question` exactly as given.
    - **Result.** One `SearchHit` per row, in order, with the same fields as `vector_search`'s:
      the chunk's `id`, `document_id`, `page`, `chunk_index`, `content` and `embedding_model`;
      the document's `source_id`, `version`, `title`, `language` and `content_kind`; the score.

    One question `TextQuery` accepts today cannot be sent at all: Postgres text cannot hold
    U+0000, so a question with a NUL character makes any statement that binds it raise. Decided
    (shesha, 2026-10-24, guide §5): `Question` will refuse it, a 422 on both routes in both
    modes, in PR 4 or a tiny PR of its own. It is not H1's to handle, and H1's tests do not send
    it.

    Not this function's job: embedding the question, refusing mixed model ids (PR 4 checks
    first, as M2's search does), fusion (H2's `rrf_fuse`), the evidence threshold and the answer,
    and filtering by language: there is none (D1), and the question is parsed with every
    candidate chunk's config, so a question in one script finds no chunk written in another.
    """
    result = await connection.execute(
        _SEARCH,
        {
            "embedding": vector_literal(query.embedding),
            "question": query.question,
            "business_id": query.business_id,
            "embedding_model": query.embedding_model,
            "audiences": list(query.audiences),
            "k": query.k,
        },
    )
    return [SearchHit.model_validate(dict(row)) for row in result.mappings()]

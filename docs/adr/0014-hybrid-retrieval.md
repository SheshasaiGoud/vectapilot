# ADR 0014 — Hybrid retrieval: full-text search next to vector search, fused by RRF

- **Status:** accepted · 2026-10-23 · chosen by shesha (M3 plan decisions D2, D6, D7, D8, D13 and
  D15: every recommendation, D15 (iii) with shesha's own number; `docs/plans/m3-pr-checks.md`,
  "Decided 2026-10-23"). Drafted by maya-tech-lead from research by ananya-rag, noah-evals,
  diego-backend and zara-safety. Built in M3 PR 2–5 and H1/H2; nothing in it is built yet.

**Context.** M2 retrieves by meaning only: H1's `vector_search` ranks a business's approved chunks
by exact cosine distance, with `business_id` in the statement's `WHERE` and RLS underneath (ADR
0004, 0008, 0009). Embeddings can rank exact tokens (a name, a price, a code) below looser
paraphrases; whether that happens on v0.1 is unmeasured. The README names M3 "RAG v2 — hybrid
search, reranking, abstention". Five constraints shape the choice. Nothing one business stores may
change another's ranking (business-context §5). ADR 0011's evidence gate is a cosine threshold, and
`SearchHit.score` is a cosine in [−1, 1]. Businesses write English, Hindi and Hinglish, later
Telugu, and Postgres 18.6 has `english`, `hindi` and `simple` configs but no `telugu` (checked by
ananya and diego on the local stack, 2026-10-23). The machine has 8 GB RAM, a 4 GB Docker VM and a
data volume 93 % used. v0.1's dev split holds about 28 questions (an estimate from the manifest's
minimums, not a count).

**Options.**
- Lexical leg: (a) Postgres' built-in full-text search (`tsvector`, `ts_rank`); (b) a BM25
  extension (ParadeDB `pg_search`, VectorChord-bm25); (c) `pg_trgm` trigram matching.
- Text-search config: (a) `simple` for every chunk; (b) per chunk, from the document's language
  through a platform map; (c) `english` for all (hard-codes a language, rule 9).
- Query: an OR of the question's lexemes, or `websearch_to_tsquery` as is (it ANDs every term).
- Fusion: RRF with a fixed k; RRF with k tuned on dev; averaging or taking the max of scores.
- Evidence under hybrid: the cosine threshold on the fused hits; a new threshold on the fused
  score; no gate.
- Snapshot: REPEATABLE READ READ ONLY; both legs in one SQL statement; READ COMMITTED documented.
- Reranking: Voyage rerank now; rerank only past a headroom count fixed before the run; none in
  M3; an LLM reranker; a local cross-encoder.

**Decision.** Built-in full-text search with a per-chunk config, an OR query, RRF with a fixed k, a
cosine gate, one snapshot, and no reranking in M3. Each part:
1. **The full-text column** (D2 (b), PR 2). Migration 0005 adds `chunks.ts_config regconfig` and
   `chunks.tsv tsvector GENERATED ALWAYS AS (to_tsvector(ts_config, content)) STORED`: the
   two-argument form (IMMUTABLE; the one-argument form is not), with `STORED` written out (PG 18
   makes a generated column VIRTUAL otherwise, and a virtual column cannot be indexed). Existing
   rows get `ts_config` from `documents.language` through the map as it stands at 0005, then
   `SET NOT NULL`; ingestion sets it at insert.
2. **The language map.** One platform data file, BCP 47 tag → regconfig: `en` → `english`, `hi` →
   `hindi`, anything else → `simple`. It names no business (rule 9), and a Postgres test checks
   that every mapped config exists. A stored `tsv` does not follow a later map change, so any
   change needs its own backfill migration.
3. **`text_search`** (D6 (a); H1, `[HAND-WRITE]`; who writes it is still open). One bound
   statement with `c.business_id = :business_id` in its `WHERE`. It returns approved chunks within
   the reader's audiences, of the question's embedding model. `c.tsv` is matched against an OR of
   the question's lexemes, parsed with each chunk's own `ts_config`, never `to_tsquery` on raw
   text. Results are ranked by `ts_rank`, with ties in M2 H1's order (source id, version
   `COLLATE "C"`, chunk index), and cut by `LIMIT :k`. `score` is the cosine to the question's
   embedding, so ADR 0011's gate keeps its meaning. Why OR: an AND of every word of a natural
   question rarely matches. Why `ts_rank`: it scores each chunk on its own, with no corpus
   statistics, so no business can move another's ranking. That is an isolation property, not only
   a simplification.
4. **`rrf_fuse`** (D6 (a); H2, `[HAND-WRITE]`, written by shesha, D5). score(d) = Σ 1/(k + rank)
   over the lists that hold d, ranks from 1, k = 60 fixed (Cormack, Clarke and Büttcher, 2009) as
   an exact `Fraction`. Ties are broken by score, then best single rank, then M2 H1's order. A
   duplicate chunk raises `ValueError`. k is not tuned: about 28 dev questions would fit noise.
   `FusedHit` wraps an unchanged `SearchHit` and adds the RRF score and the rank in each list, so
   the types keep an RRF score (about 0.016–0.033) away from `rag_min_score`.
5. **The hybrid search** (PR 4). The question is embedded once. One business transaction holds the
   model check, `vector_search` and `text_search` at `rag_candidate_k`, and `rrf_fuse` limited to
   it. The top `rag_context_k` fused hits go to the answer. `AnswerRun.hits` is the fused list in
   fused order, as plain `SearchHit`s (a proposal, confirmed in PR 4), so M2's harness and scoring
   do not change. Settings are platform defaults, unmeasured: `rag_retrieval_mode` `vector | hybrid`
   (default `vector`), `rag_candidate_k` 20 (provisional; at least `rag_context_k` and 10, at most
   `MAX_K`), `rag_rrf_k` 60. Vector mode is M2's path unchanged.
6. **One snapshot per search** (D8 (a)). In hybrid mode the transaction's first statement is
   `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY`, then `set_config` as today. Both
   legs then see the same committed data, so an approval between them cannot put a retired and a
   new version of one source into one fused list. A read-only transaction writes nothing, so it
   meets no serialization failure. This amends ADR 0011 decision 5 for hybrid mode only.
7. **Evidence under hybrid** (D7 (a)). Evidence is the fused hits whose cosine is at least
   `rag_min_score`, re-tuned on dev only. Confidence v0 is unchanged. FTS-only hits (in the text
   list, not the vector list) are counted. A keyword-stuffed chunk with a low cosine is never
   evidence, and the gate fails closed.
8. **Adoption** (D15). The default stays `vector` until a paired dev comparison against M2's
   vector baseline meets a rule fixed in writing before the first hybrid run. Per business,
   "vector hit, hybrid miss" must be ≤ "hybrid hit, vector miss", and no safety row that vector
   did not answer may be answered under hybrid. Variants: hybrid at M2's chosen `rag_min_score`
   with `candidate_k` 20, plus at most one `rag_min_score` variant. All of M3's paid runs together
   stay within $5.00, each within M1's $1.00 cap.
9. **Not in M3.**
   - No GIN index on `tsv` and no HNSW index unless PR 8's EXPLAIN shows they would be used and
     are needed: `@@` and `<=>` are not leakproof, so under RLS the app role may never use them
     (to confirm in PR 8).
   - No BM25 extension: a new extension, and corpus-wide statistics shared across businesses.
   - No `pg_trgm` until dev misses show a need.
   - No Telugu config: Postgres has none, so `simple` is the fallback.
10. **No reranking in M3** (D13 (c); a deviation from the README's M3 row). PR 5 reports the
    headroom count: scorable questions whose fused list has hit@10 = 1 and hit@5 = 0, listed by
    id. It is the measured reason for the deviation and the trigger to revisit it. Each business
    has roughly 60–65 chunks (ananya's count: 129 in total at 1200/150), so 2 × 20 candidates
    already cover most of a corpus. Never in M3: a
    local cross-encoder (the machine) or an LLM reranker (a paid call per question and a new
    injection surface).

**Consequences.**
- Two retrieval legs must each keep the business filter. The canary suite covers the full-text
  leg and the fused list, owner-connection clause checks catch a leg that relies on RLS alone, and
  the stricter canary gate lands before H1 (PR 3).
- The vector path, the API's response shapes and `score` stay as in M2, so M2's tests and baseline
  stay valid.
- Migration 0005 rewrites `chunks` (small today). `hindi` has no stop list, so OR queries match
  many Hindi chunks. There is no lexical match across scripts, and romanized Hinglish in an `en`
  chunk relies on English stemming.
- Hybrid adds a statement and a fuse to every search. Its latency is measured locally in PR 8,
  not claimed here.
- On v0.1, "no measurable difference" is a likely and acceptable result. Hybrid becomes the
  default only through D15's rule, recorded with `compare`'s counts.

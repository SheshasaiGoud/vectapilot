# ADR 0011 — Cited answers v0: the answer contract and abstention

- **Status:** **accepted** · 2026-10-22 · shesha accepted every recommended default. Drafted by
  ananya-rag in M2 PR 6 (`m2/search-and-answer`) after maya-tech-lead's review (F2). The options
  came from the plan's "Open for shesha" list (`docs/plans/m2-pr-checks.md`, PR 6); each item
  below records the one chosen (**chosen**) and the ones considered.

**Context.** PR 6 builds the first customer-facing answer: `POST /v1/answer` retrieves a
business's approved public chunks (H1's `vector_search` behind `retrieval/search.py`), asks the
`large` alias through the gateway's `complete_structured` (ADR 0006), and returns the answer with
its sources. M3 (hybrid search, reranking, a judge, the trace span), M4 (the router), M7
(guardrails), M8 (the widget, hand-off and staff review) and PR 8's eval harness (`AnswerRun`) all
build on its shape. The rules it must keep: no source, no claim; weak evidence means an honest
"I don't know" or a hand-off (business-context §3.5: "Weak or conflicting evidence: do not
answer"); retrieved text is data, never instructions (CLAUDE.md rule 5); everything is scoped by
`business_id` (§5, ADR 0009); only measured numbers (rule 3). ADR 0006 deferred Anthropic's
native Citations to M3 and notes that native structured output cannot be combined with them.

**Decisions on the open questions** (2026-10-22, shesha):
1. *Threshold value.* **Chosen: (a)** `rag_min_score` = 0.3, **provisional and unmeasured**,
   tuned on eval v0's dev split in PR 8, never on the test split. Considered: (b) no threshold
   until measured (every question reaches the model); (c) per business, once per-business
   configuration exists.
2. *Weak-evidence outcome.* **Chosen: (a)** `dont_know` by default, `handoff` by setting, until
   M8 (when a hand-off can notify staff). Considered: (b) `handoff` by default (§3.5's proposed
   default), only a label before M8; (c) per business.
3. *One unverified citation.* **Chosen: (a)** drop the whole answer: the text cannot be split into
   the claims each id supports. Considered: (b) drop only the unverified source and keep the text
   (keeps the claims that id was meant to support); (c) one repair call asking the model to cite
   again (another paid call, and a second chance for an injected id).
4. *Answer language.* **Chosen: (a)** the question's language, **provisional until Q6**
   (business-context §9) is decided. Considered: (b) the business's configured language; (c) the
   source's language.
5. *Isolation level for the model check and the search.* **Chosen: (a)** READ COMMITTED,
   Postgres' default: a reindex that commits in between only shrinks the candidates, because H1
   compares only chunks of the question's model. Considered: (b) REPEATABLE READ, one snapshot for
   both (not needed for correctness).
6. *Per-business configuration.* **Chosen: (a)** platform defaults in Settings (`rag_min_score`,
   `rag_context_k`, `rag_weak_evidence_outcome`) until a per-business configuration loader exists;
   nothing names a business (rule 9). Considered: (b) build the loader now (business-context §9).
7. *Question limit.* **Chosen: (a)** 2,000 characters, counted before stripping, a platform
   default, unmeasured. Considered: (b) another limit, or one per business.
8. *Endpoint shape.* **Chosen: (a)** `POST /v1/answer` with a JSON body (the question stays out
   of URLs) and `GET /v1/kb/search?q=` (access logs drop query strings, `log.py`). Considered:
   (b) search as a `POST` body too; (c) wait for M8's conversation endpoints.
9. *Trace id plumbing.* **Chosen: one trace id per request, also as an `X-Trace-Id` header (b).**
   The route makes the id before the service runs; the `embedding_call`, `llm_call` and
   `rag_answer` records carry it; the 200 body keeps it as `trace_id`, and every response from the
   service step sets `X-Trace-Id`, the typed 422 `question_too_long`, 502 and 503 included, so a
   failed request can be joined to its records. It is a random id the server made, never input.
   A request refused before the service step (400 for the business, or a 422 for the request
   itself) has no trace id and no header, because nothing was recorded for it; a bug (500) has
   none either. A browser client on another origin will need `Access-Control-Expose-Headers:
   X-Trace-Id` when CORS arrives (M8). Considered: (a) the body only (a typed error could not be
   joined); (c) W3C `traceresponse` once M3 adds spans.
10. *Typed errors vs `dont_know` for `reindex_required` and model failures.* **Chosen: (a)**
    typed errors with fixed messages (`503 reindex_required`, `503 model_unavailable`,
    `502 answer_failed`): failures stay visible as failures. Considered: (b) the weak-evidence
    outcome with the reason only in the record (failures would look like abstentions).
11. *The paid HTTP path before M7's budgets* (maya, F3). **Chosen: (b)** the API server refuses a
    paid `llm_provider` (any but the fake) unless an explicit opt-in setting is on, default off,
    until M7 adds budgets. diego implements it and records it as an amendment to ADR 0006
    ("Exposure"), which is the authority on the setting (in his change: `api_allow_paid_llm`,
    `VECTAPILOT_API_ALLOW_PAID_LLM`). PR 8/9's live evals call `AnswerService` through the harness
    under M1's reservation cap. Considered: (a) as first built (every answer with evidence an
    uncapped `large` call); (c) (b) plus a per-process USD cap in `AnswerService`.

PR 6 ships as **one PR** (search and answer together).

**The contract.**
- **Outcomes.** `answered | dont_know | handoff`. `answer` is null unless `answered`; the client
  shows the business's own "I don't know" or hand-off text (none is hard-coded).
- **Threshold before any model call.** Evidence = the hits scoring at least `rag_min_score`. No
  hits, or none at the threshold, means no model call at all, and the weak-evidence outcome.
- **Our own chunk ids as citations**, not native Citations: the model returns `ModelAnswer`
  (`outcome`, `answer`, `citations`) through the gateway's provider-neutral structured output,
  and each id is checked against the prompt. Native Citations would need a second output path in
  the gateway (deferred to M3 by ADR 0006), where this choice can be revisited.
- **Fail closed.** Shown only if the model said `answered`, wrote a text, cited at least one id,
  and every cited id is a chunk this prompt held (retrieved for this business in this request).
  Otherwise the weak-evidence outcome, with no text (decision 3). A model that says `dont_know`
  is never shown, whatever text or citations it wrote.
- **Sources** are exactly the cited chunks, in citation order, with the fields a citation needs
  (chunk id, document id, source id, version, title, page, chunk index, language, content kind,
  text, score).
- **Confidence v0** = `{value: the top retrieval score over all hits, basis:
  "top_retrieval_score", calibrated: false}`: cosine similarity, not a probability.
- **The prompt.** Versioned (`PROMPT_VERSION`; `PROMPT_FINGERPRINTS` makes CI refuse a changed
  prompt or `ModelAnswer` schema under an old version). Rules in the system prompt; each chunk in a
  block whose markers carry a boundary that no untrusted text contains (deterministic, checked);
  documents first, the question last.
- **One `rag_answer` record per answer**, metadata only (never question, chunk or answer text),
  also on error: a new log-record family next to ADR 0007's `llm_call` and ADR 0008's
  `embedding_call`, joined to them by `trace_id` (and, from a response, by `X-Trace-Id`). A log
  line only; a table with RLS would be a later decision. `route` is `rag` until M4.

**Consequences.** An answer is shown only with a source that was retrieved for that business, so
an injected or invented id never reaches a customer, whatever the model does. The price: one bad
id costs the whole answer, so `dont_know` rates rise until the prompt and threshold are tuned in
PR 8 (measured there, not claimed here). The threshold and confidence are uncalibrated until
labelled eval data exists. PR 6 has **no safety check** of its own: §3.5's emergency and
safety triggers (and the 100 % safety-escalation recall gate, business-context §6) belong to M4's
router and M7's guardrails, so v0 must not face real customers before them. The `rag_answer`
record and `AnswerRun` become a contract that PR 8's harness reads; changing them later needs a
new `record_version`. With decision 11, the API server is not a path to paid model calls unless
someone opts in on purpose; once opted in, each answer with evidence is still an uncapped call
until M7's budgets.

**Amended 2026-10-23 (shesha, M3 plan D1, D3, D7, D8, D11, D12; `docs/plans/m3-pr-checks.md`;
[ADR 0014](0014-hybrid-retrieval.md)).** The decisions above stand except where these lines say
otherwise.
- **Decision 4 is no longer provisional.** Q6 (business-context §9) is answered (D1 (a)):
  translation is allowed. Retrieval has no language filter, the answer is in the question's
  language, and each source shows its own `language`, as the sources already do. This is a
  platform default and needs no code. Per business (D1 (c)) once a configuration loader exists;
  zara may tighten it for safety content in M7. A question in a language its business does not
  list still expects `handoff` (ADR 0012, 5).
- **Decision 5 in hybrid mode** (D8 (a); ADR 0014 decision 6). The search transaction is
  REPEATABLE READ READ ONLY. With two legs, an approval committed between them could otherwise put
  a retired and a new version of one source into one answer. Vector mode stays READ COMMITTED.
- **The threshold under hybrid** (D7 (a); ADR 0014 decision 7). Evidence is still the hits whose
  cosine is at least `rag_min_score`, re-tuned on dev only. `score` stays the cosine and is never
  the RRF score.
- **Native Citations are revisited and declined for M3** (D11 (a)). Our own chunk ids stay: a prompt
  or output-path change would end comparability with M2's baseline (D3). Per-claim citations
  return with M7's grounding check.
- **`traceresponse`** (decision 9 (c)) follows the trace span to M9 (D12 (a); ADR 0007 amendment).
  An inbound `traceparent` stays unread, so the trace id stays server-made; revisited with M8's
  widget.
- **The `rag_answer` record** (a proposal, confirmed in M3 PR 4). New optional, metadata-only fields
  (retrieval mode, hits per leg, FTS-only hits, per-stage durations) keep `record_version` 1.
  ADR 0007's rule ("additive; a breaking change bumps it") governs the Consequences' "changing
  them later needs a new `record_version`": only a breaking change needs one. A test shows that
  M2's records still validate.

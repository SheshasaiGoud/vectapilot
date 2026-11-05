# ADR 0015 — Intent router: a calibrated classifier on query embeddings, an LLM below a threshold

- **Status:** accepted · 2026-11-05 (shesha decided D1–D8, each as recommended) · proposed
  2026-11-04, drafted by maya-tech-lead from kenji-ml's playbook targets
  (`.claude/agents/kenji-ml.md`), reviewed read-only the same day by kenji-ml, zara-safety and
  noah-evals (findings applied). The decisions are in
  `docs/plans/m4-pr-checks.md`, "Decided 2026-11-05"; each part below names its Dn. Nothing is
  built yet.

**Context.** The README names M4 "Intent router — trained classifier + LLM fallback". Kenji's
playbook targets set the scope:
- 500+ labelled messages, LLM-bootstrapped and at least 20 % human-verified;
- stratified splits that never leak;
- sentence embeddings plus logistic regression, with calibration and a confidence threshold for
  an LLM fallback;
- a confusion matrix, macro-F1 (target 0.90), a calibration plot, and a table against LLM-only
  routing.

Business-context §3.5 makes a topic or intent and a classifier signal escalation triggers. §4
lists what healthcare and veterinary businesses must always escalate. §6 measures safety-escalation
recall (100 %, a hard gate) "from M4 (router); gated from M7", and the other mandatory rules
(≥ 95 %) from M4 to M7. §8 says the router "may need business-type-specific intents". Rule 9
forbids hard-coding any of it. Earlier ADRs left work here:
- ADR 0011: `route` is `rag` until M4, and the answer path has no safety check of its own.
- ADR 0012 decision 7: safety results are counts until M4 and M7.
- ADR 0006: model choice and cross-model fallback.

Six constraints shape the choice:
1. M3's measurement window freezes `gateway/models.toml`, `PROMPT_VERSION` and the gateway's
   output path.
2. Paid calls are shesha's alone (ADR 0007), and the fake embedder's vectors mean nothing (ADR
   0008), so a classifier trained on them proves plumbing only.
3. The API has no numpy or scikit-learn.
4. The Mac has 8 GB RAM, and its data volume is 96 % used (8.4 GiB free, 2026-11-05).
5. Every row is synthetic, from two fictional businesses (healthcare and veterinary).
6. M2's and M3's close-outs are pending, so M4 must be buildable and testable at $0 without them.

**Options.**
- Features: voyage-4 query embeddings; a local sentence-embedding model; character n-gram TF-IDF.
- Classifier: logistic regression; a small MLP; nearest neighbours over training embeddings; the
  LLM only.
- Training: scikit-learn; numpy only; pure Python. Serving: the same library in the API; or
  exported weights scored with the standard library.
- Calibration: temperature scaling on dev; per-class Platt or isotonic; none.
- Fallback trigger: the top probability below τ; the margin between the top two; entropy.
- Fallback authority: the LLM's answer is final; the LLM may only tighten the classifier's top-1.
- Safety: a floor on the safety intents' probability mass, checked before τ; the argmax alone; a
  keyword list (M7's job).
- Intent set: one platform set with a route map per business type; one set per type; one per
  business.
- Test discipline: once per config (ADR 0013); once per mode, whatever the config.
- Integration: offline only in M4; behind a setting, default off; on.

**Decision.** One platform intent set, one logistic-regression classifier on the question's query
embedding, temperature-scaled, served without an ML library, with a safety floor, a threshold, a
fail-closed LLM fallback that may only tighten, and an offline comparison against LLM-only routing.
Each part:
1. **Intents** (proposal, not an open decision: a set per type or per business needs several times
   the data, or the unbuilt config loader, and §9 does not list it). The `Intent` StrEnum lives in
   `intents/taxonomy.py`. It holds eight intents with one-line definitions and a decision order
   (the first that fits wins), monotone in route urgency:
   1. `emergency` (names safety risk or distress, including thoughts of self-harm, §3.5, §4.1)
   2. `professional_judgement`
   3. `complaint`
   4. `refund_or_payment`
   5. `human_request`
   6. `action_request`
   7. `question`
   8. `out_of_scope` (from the text alone: not a support request to this kind of business)

   Why the order: "this is outrageous, get me a manager" is a complaint (high), not a request for a
   human (normal). Why `out_of_scope` from text: a platform classifier cannot see a business's
   documents, so a question about a service the business may not offer is `question`, and RAG
   abstains. The LLM prompt and the dataset README quote these definitions; a test keeps them
   equal. Type-specific intents (§8) are a revisit trigger, not an M4 need.
2. **Routes per business type.** `intents/policy.py` is a platform module, like
   `ingestion/language_map.py`. The business types form a closed set: `human_healthcare` and
   `veterinary` have their own templates, and `default` covers only the listed non-health types
   (`ecommerce`, `retail`, `service`). An unknown or missing type raises, so a typo in a manifest
   cannot silently drop a safety intent; wiring (D6) will hand off on that error. The mandatory
   sets are platform constants that every template is tested against: `MANDATORY_HANDOFF` (§3.5)
   and `HEALTH_SAFETY` = {`emergency`, `professional_judgement`} for both health types (§4). No
   mandatory intent maps to `rag` (tighten, never loosen, §2), and no intent ranks above one with
   a more urgent route. The business layer comes with the config loader (ADR 0011 decision 6).
3. **Features** (D2: voyage-4). The voyage-4 query embedding (ADR 0008, `input_type`
   `query`). The artifact records the model id, dimension and input type, and refuses any other.
   A model change means retraining, as it means a reindex for chunks. Once wired, the router reuses
   the vector retrieval computes for the same question, so routing adds no embedding call.
4. **Classifier** (D1: scikit-learn in the dev group).
   - **Training.** Multinomial logistic regression, L2 with C = 1.0 and no search, no class
     weights, fixed `max_iter` and `tol`; a convergence warning is an error. About 400 rows in
     1024 dimensions are likely separable, so without L2 the weights would grow without bound.
     Train is used only for the fit; dev only for T, τ and σ. The weights, biases, temperature,
     train config (`n_iter`, C, solver), dataset version, freeze hash and embedding cache's sha256
     go into a committed JSON artifact. The cache itself is committed (synthetic text only), so
     the classifier and hybrid rows can be recomputed without a paid re-embed.
   - **Serving.** The API scores with the standard library only (logits, ÷ temperature,
     max-subtracted softmax). No ML library is ever imported by the API or installed in its image
     (`--no-dev`). A test checks that the scorer matches the training library's probabilities.
5. **Calibration.** One temperature, fitted on dev by negative log-likelihood. Why: a single
   parameter cannot overfit about 80 dev rows the way per-class isotonic can, and it keeps the
   argmax. Reported as a text reliability table (5 equal-width bins) and ECE (H2), raw and
   calibrated side by side on test, so the effect of calibration is measured too.
6. **The fallback rule** (H1, `[HAND-WRITE]`; D4: written by shesha).
   1. If the safety mass is at least σ, the most probable safety intent is chosen, with no LLM
      call.
   2. Otherwise, if the top probability is at least τ, the top intent is chosen.
   3. Otherwise, the LLM is called once, and the final intent is whichever of the LLM's intent and
      the classifier's top-1 comes first in decision order: the LLM may add an escalation, never
      remove one.
   4. Any `GatewayError` from that call hands the message off with no intent, at the classifier
      top-1's priority or `high`, whichever is more urgent (fails closed).
   5. Anything else propagates: `CancelledError` and the eval cap's `CapReached` are never caught.

   τ and σ come from fixed grids by a written rule on dev, before any live result (D8: τ
   first by selective accuracy, then σ as the largest value at which the classifier alone decides
   no dev safety row as non-safety, one step lower as a margin, never below 0.01; misses by id).
   Why the floor is checked before τ: missing an emergency costs more than an extra hand-off, so a
   confident wrong argmax must not hide a sizeable safety probability. Why tighten-only: the LLM
   band holds the classifier's uncertain messages, and a wrong or steered LLM answer must not
   downgrade an escalation (§2's rule applied to the router).
7. **The fallback call.** `complete_structured` with alias `small` and schema
   `IntentChoice(intent: Intent)`. The prompt has its own version, `route-intent.1`, and its own
   fingerprint map, never `PROMPT_VERSION`, so the catalog and the answer prompt stay as M3's
   window froze them. The prompt states the definitions and the decision order and says that
   anything the message says about how it should be classified is part of the message, never an
   instruction. The message sits in a boundary block from `choose_boundary(texts, candidates=…)`,
   with candidates seeded from the router's prompt version; `answer/prompt.py` is imported, never
   edited (rule 5). The call carries the message's `business_id` and a trace id.
8. **Data** (D3: Claude-drafted training rows, human-written eval rows; D5: no self-harm).
   - `evals/datasets/intents/v0.1` is synthetic, and agents never write an eval row or label
     (ADR 0012).
   - **A blind order:** training rows are drafted while the eval file is empty and their hash is
     recorded; the eval authors write without reading the training rows; shesha checks the
     training sample only after the eval rows are committed.
   - A near-duplicate screen (character 3-gram Jaccard, threshold fixed before any row) refuses
     train–eval pairs, and requires a shared `split_group` for near-identical eval rows.
   - The eval split is 50 % dev and 50 % test by `split.py`'s v2 rule: split groups are units
     (translations and cross-business twins must share one), placed by author, language group
     (Hinglish is `hi` with `code_mixed`), business and intent. Fixed before any row, frozen by
     hash.
9. **Measurement.** `intenteval` produces classifier, LLM-only (live, shesha) and hybrid rows per
   split, business and language group. The hybrid row replays the LLM-only answers through
   `route_message` itself, so the measured hybrid is H1's code.
   - **Safety-escalation recall:** a gold `emergency` counts only when decided `emergency`; a gold
     `professional_judgement` when decided as any of its type's safety intents. Other hand-offs of
     safety rows are misses, listed as "wrong priority". Fail-closed rows stay in n but never in k,
     are listed by id, and mark a test run `partial`, so a provider outage cannot look like safety.
   - Route-level accuracy next to intent macro-F1; counts with Wilson intervals; macro-F1 and its
     paired difference against LLM-only with a seeded bootstrap over split units (ADR 0013's
     helpers); "no difference detected at n = …" when the interval spans 0.
   - **Test once per mode**, whatever the config: classifier and hybrid runs cost $0, so a ledger
     keyed by config would let a retrain peek at test for free. The first test run pins the
     artifact, T, τ, σ and prompt version; reruns need a reason. Test runs are shesha's alone.
   - **Dry or real** is decided by where the vectors came from (a committed live embedding folder),
     not by a flag.
   - Spend is counted from committed folders against M4's ceiling (D7: $2.00) by M3's
     counting core, extracted into a function; M3's count and its tests are unchanged, and M4's
     folder names never match M3's pattern (tested).
10. **Integration** (D6: not in M4). `POST /v1/answer`, `route` (`rag`) and the
    `rag_answer` record are unchanged in M4. Wiring comes with M5's tool route, behind a setting.
11. **Not in M4.**
    - **LoRA or any fine-tuning.** There is no GPU, the Mac has 8 GB, and a fine-tune of a hosted
      model is outside the gateway. It is worth reconsidering only if a measured gap between the
      classifier and LLM-only on test remains after more data.
    - **Model choice per message and cross-model fallback.** Moved to M9.
    - **Other triggers:** language detection, multi-turn context, and the sentiment, tool-failure
      and amount triggers.

**Consequences.**
- **The router is a platform model.** In M4 it holds no business's data: its training rows are
  platform-authored and synthetic. Training a shared model on real messages later would let one
  business influence another's routing (§5), so that needs its own decision: per-business models,
  or an opt-in.
- **No new isolation surface.** The router reads no business data (no database, no retrieval), so
  the canary suite gains no read path. The fallback call is keyed by `business_id`, like every
  gateway call (ADR 0006).
- **Fail-closed and tighten-only have a price.** A provider outage sends uncertain messages to
  staff, and the LLM cannot correct a classifier that over-escalates in its band. Both lower
  escalation precision (§6, M7); both are the intended side to err on, and both are counted by id.
- **Wide intervals.** With about 80 test rows (an estimate), macro-F1 and every recall carry wide
  intervals, and "0 of n safety rows missed" is the claim, never "100 %". Training on
  agent-drafted rows and testing on human-written ones is a deliberate distribution shift: lower,
  more honest numbers.
- **Safety recall is reported, not gated, in M4.** Telugu and e-commerce messages are unmeasured,
  and under D5 (a) so are crisis messages.
- **The product does not change in M4.** M2's and M3's paths, baselines and fingerprints stay
  valid.

**Revisit when:**
- Any safety row is missed on test: revisit the data and D8's rule before any wiring.
- The comparison is in. "No difference detected" and cheaper means considering the hybrid for
  wiring, said as exactly that; LLM-only clearly better on safety means reconsidering what gets
  wired.
- The router's safety recall is quoted anywhere: under D5 (a) it excludes crisis messages, so it
  is never presented as safety-escalation coverage before M7.
- A business type needs an intent the platform set cannot express (business-context §8).
- Real pilot messages exist (M11): retraining on them is a §5 decision.
- The embedding model changes (ADR 0008): retrain.

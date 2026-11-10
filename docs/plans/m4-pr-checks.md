# M4 — acceptance checks per PR

> **Status: planned 2026-11-04; decided 2026-11-05 (shesha, D1–D8).** Drafted by maya on `m4/plan` in lean
> mode (one builder and one combined review per PR; routine choices are proposals the team applies
> unless that review changes them). **Reviewed read-only on 2026-11-04 by kenji-ml, zara-safety
> and noah-evals; their findings are applied.** shesha decided D1–D8 on 2026-11-05 ("Decided
> 2026-11-05"), each as recommended, and [ADR 0015](../adr/0015-intent-router.md) is Accepted. M2 and M3
> are **not closed** (`support_qa` v0.1 has 0 questions, no live run, no baseline:
> [`m2-close-out.md`](m2-close-out.md), M3 PR 9). M4 does not wait: it has its own dataset, no
> database and no retrieval, and every PR is built and tested at $0 (fake embedder and provider).
> Only the close-out's live steps wait, for M2 close-out step 11 (the Voyage and Anthropic keys).

"PR 0–5" are plan step numbers, not GitHub pull-request numbers; each PR copies its section into
its own acceptance check. H1 and H2 are M4's `[HAND-WRITE]` pieces (rule 2). Tags: **[Dn]** =
depends on open decision Dn · **[proposal]** = routine, applied unless its PR's review changes it
· **[carried]** = from the named earlier plan, ADR or business-context section. Sizes are
estimates, never measurements. No number in this file is a result (rule 3).

## What M4 delivers
The roadmap's "Intent router — trained classifier + LLM fallback", in the smallest form that still
proves the playbook's point: a cheap, calibrated classifier routes the obvious messages, the LLM
handles only the uncertain ones, and both are measured against LLM-only routing.
1. **Intents and routes from platform configuration:** one intent set, a route map per business
   type, mandatory escalations never loosened [proposal, ADR 0015 decision 1; carried §2, §3.5, §8].
2. **`intents` v0.1** (synthetic): training rows and human-written eval rows, kept apart by
   author, by order of writing and by a near-duplicate screen; split rule fixed before any row [D3].
3. **A classifier:** L2-regularised logistic regression on voyage-4 query embeddings [D1, D2],
   with temperature scaling fitted on dev, scored by the API with the standard library only.
4. **The fallback rule (H1):** a safety floor, then the threshold, then the LLM (`small` alias),
   which may add an escalation but never remove one. An LLM failure hands off (fails closed).
5. **The comparison:** classifier only, LLM only and hybrid on the same messages, per business and
   language, by the scoring rules below. The metric functions are H2.
6. **A first §6 safety-escalation recall** at router level, every miss by id; gated from M7 [§6].

**How we will know M4 worked** (PR 5):
- `intents` v0.1 is frozen; the verified share of training labels is recorded as `x of n` [D3].
- The artifact and the embedding cache are committed with dataset version, commit and train
  config, so the classifier and hybrid rows can be recomputed from the repo at $0.
- Committed: classifier, LLM-only and hybrid rows on dev, and on test once per mode (ledgered);
  for test, the comparison, confusion matrices and reliability tables per business and language,
  and safety-escalation recall as `k of n` with every miss by id.
- M4's live spend, summed from the committed folders, is within D7's ceiling.
- shesha's note `docs/notes/m4-what-i-measured.md`, the H1/H2 explain-backs and the M4 study PDF.

"No difference detected between the classifier and LLM-only" is an acceptable, honest result. So
is "macro-F1 0.90 not shown at 95 % confidence".

## Scoring rules (fixed before any row; the H2 guide and the harness's tests pin them)
- **Safety-escalation recall** (§6's hard-gate metric). A gold `emergency` row counts only when
  decided `emergency` (urgent hand-off with guidance, §3.5); a gold `professional_judgement` row
  counts when decided as any of its business type's safety intents. Any other hand-off of a safety
  row is a miss, printed next to the misses as "handed off, wrong priority: <ids>". H1's tests
  and D8's dev curve use the same definition.
- **Other mandatory rules' recall:** routed to `handoff` at any priority, "k of n, of which j
  fail-closed". **Route-level accuracy** (`rag`, or `handoff` × priority) sits next to intent
  macro-F1: the route is what the product acts on.
- **`fail_closed`** (the LLM raised a `GatewayError`) is a hand-off with no intent: its own column
  in the confusion matrix, wrong for every intent metric, scored by its actual route at route
  level. Safety recall's k counts only rows the classifier or the LLM's answer decided;
  fail-closed rows stay in n, listed by id as "not decided" (a context line counts them). Their
  count per mode and business comes before any rate; LLM-only mode follows the same rule; any on
  test marks the run `partial` [carried `evalrun/report.py`: `error` apart from `handoff`].
  `CapReached` stops a run, never a hand-off.
- **Macro-F1** over the fixed eight intents, the guide's 0/0 rule, exact `Fraction`s. The bootstrap
  resamples split units (seed and B in run.json; the number of grouped rows shown). Macro-F1 and
  its interval are shown for a slice only when every intent has at least 5 gold rows **and** at
  least 5 split units in it; otherwise counts and the confusion matrix only: "macro-F1 not shown
  (n too small)" with the reason (which intents fall short, and by what) [decided 2026-11-09].
- **Calibration:** 5 equal-width bins of top probability, each with n, mean confidence and
  accuracy; ECE with its n and "biased upward at small n"; raw (T = 1) and calibrated side by side
  on test; per business as counts only [proposal].
- **Breakdown** per business × language group (`en`, `hi`, `hi-code-mixed`), counts with Wilson
  intervals; an empty cell reads "not measured". Channel: "not applicable offline" [carried §6].
- **The comparison is paired:** per business and language, discordant rows by id (classifier
  right and LLM wrong, and the reverse; hybrid likewise), safety rows apart [carried `compare.py`],
  and a seeded paired bootstrap of the macro-F1 difference over split units. When it spans 0 the
  wording is "no difference detected at n = …", never "as good as".
- **Cost and latency:** cost per 1,000 messages (`Decimal`) with the router's own embedding call,
  and marginal when reusing retrieval's vector (a projection); classifier latency as embedding +
  scoring, scoring alone as the projection; p95 only at n ≥ 20 [carried M1 H3]; labels "local:
  shesha's machine and network" and, for hybrid, "composed from replayed calls". Fallback share
  per intent; cost and fallback share are "on the eval mix, not a traffic estimate".
- **Test discipline.** The once-only key is (dataset, version, freeze hash, mode), not the config:
  $0 runs would otherwise allow a free retrain as a "new config". A second test run of a mode
  needs `--test-rerun-reason`; the note reports the first ledgered run and lists reruns. Before
  any test run, one ledger entry pins the artifact sha256, T, τ, σ and `ROUTER_PROMPT_VERSION`,
  all committed in HEAD; hybrid on test refuses anything else. Every test run, $0 ones included,
  is shesha's, typed at a terminal; agents never run test. LLM mode on test needs a committed,
  complete live LLM dev run with the same fingerprint [carried ADR 0013 decision 5].
- **Dry or real.** LLM mode is live only with `--live`, as in M2. A classifier or hybrid run is
  real only when the artifact's embedding cache and the eval vectors match a committed
  `-live-embed` folder by sha256, whatever the flags. Anything else writes to `evals/reports/tmp/`
  under "DRY RUN — fake embedder/provider: plumbing only, never a result"; test is refused.

## Out of scope, and playbook deviations
- **Wiring into `POST /v1/answer`** [D6]: `route` stays `rag`, `rag_answer` is unchanged, and
  there is no `route_decision` log record (no product path to log) [proposal].
- **Some §3.5 triggers:** language detection (§3.3's "unsupported language → hand-off"),
  multi-turn context, sentiment, tool failure and amount limits: M5, M7, M8 or the config loader.
- **Self-harm messages,** unless D5 says otherwise. ADR 0012 decision 6 (`m2-pr-checks.md:366-368`)
  kept them from friends only and left sensitive safety questions to shesha; M4's reason is new:
  live provider calls and their 2-year flagged retention (ADR 0007, line 71). In every D5 option
  `emergency`'s definition names thoughts of self-harm (§4.1).
- **Data:** no Telugu rows [carried M3], no e-commerce rows (no fictional shop), no real message.
- **M3's measurement window** [carried M3 D3]: no change to `gateway/models.toml`,
  `PROMPT_VERSION` or the gateway's output path; M4 also leaves `answer/prompt.py` unedited
  [proposal]. The fallback uses `small`.
- **Deviations** [proposal unless tagged]: training rows drafted by Claude at $0, not bootstrapped
  by an LLM call [D3]; a text reliability table plus ECE instead of a calibration plot (no
  matplotlib); no LoRA stretch (ADR 0015 decision 11); model choice per message and cross-model
  fallback, left to M4 by ADR 0006 (lines 62 and 94), move to M9; the hybrid row replays the
  LLM-only run's answers through `route_message` instead of a second paid run, labelled "replayed".

## Facts this plan relies on (checked 2026-11-04; the reviews' citations re-checked 2026-11-05)
- **Answer path and gateway:** `answer/types.py:37`, `:94`: `route` is `rag` until M4 (ADR 0011).
  `answer/prompt.py:40` `PROMPT_VERSION = "rag-answer.1"`, `:45` `PROMPT_FINGERPRINTS`, `:117`
  boundary candidates seeded from `PROMPT_VERSION` (`choose_boundary` takes others).
  `gateway/models.toml:15` `catalog_version = "2026-10-14.1"`, `:65` `small` = `claude-haiku-5-5`
  ("routing labels"); `gateway/gateway.py:323` `complete_structured` makes at most one repair.
- **Embeddings and dependencies:** voyage-4, 1024 dimensions, `InputType` `document | query`. No
  numpy or scikit-learn in `uv.lock`; the image runs `uv sync --locked --no-dev` (`Dockerfile:16`,
  `:21`); `pyproject.toml:83` turns warnings into errors.
- **Eval tooling:** `bootstrap_interval` takes any items and any `Fraction` statistic
  (`evalrun/intervals.py`); `evalrun/budget.py:63` `CapReached` is not a `GatewayError`;
  `evalrun/cli.py:943-949` refuses only a second test run of the same config fingerprint, and
  `:494` picks live or dry by `--live`; `evalset/split.py` v2 places split-group units by author
  type, language group, business, category; Hinglish is `hi` + `code_mixed`
  (`evalset/questions.py:45`, `:251`); `evalrun/spend.py:67` `LIVE_FOLDER` matches only
  `-live-dev|test` names, and its ceiling, branch and reason are M3's own constants.
- **Businesses:** the type is a free slug in `data/sample/<id>/manifest.json`
  (`evalset/corpus.py:170`): `sunrise-clinic` `human_healthcare` (`en, hi`), `paws-and-co`
  `veterinary` (`en, te`); §7 uses `ecommerce`. **Disk:** 96 % used, 8.4 GiB free (2026-11-05).

## Order of work
```
Decide:   D1–D8 (shesha): PR 1 needs D3, D5 · PR 3 D2 · PR 4 D1, D7, D8 · H1/H2 D4
Now ($0): PR 0 plan · PR 1 taxonomy, policy, dataset format and tools
Then:     PR 2 training draft (after PR 1, before any eval row) · PR 3 core + H1 scaffold
          (after PR 1) · PR 4 eval harness + H2 scaffold (after PR 3) → H1 (shesha), H2 (Claude) [D4]
shesha:   eval rows, blind (after PR 2 merges; overlaps PR 3–4), then the training sample check
Measure:  PR 5 close-out (after PR 4, H1, H2, the freeze and M2 step 11's keys)
```
**The blind order** [D3 (a); proposal]: train and eval must not leak by author or by order.
1. PR 2's rows are drafted while `eval.jsonl` is empty, and its builder never opens that file. At
   merge, `train.jsonl`'s sha256 goes into the manifest (`train_draft_sha256`).
2. kenji and zara review PR 2's text; shesha approves it from `report`'s table, unread.
3. shesha (and friends, from a signboard-level brief) write every eval row without reading
   `train.jsonl`; shesha reads training rows only in PR 5 step 2's sample, after the eval rows
   are committed.
4. A later training row is drafted without opening `eval.jsonl`, and the note says so.

5 team PRs and 2 hand-writes, each reviewed by shesha; M1–M3's open human steps are not M4's.

## PR 0 — `m4/plan` (maya)
- This file, ADR 0015, CLAUDE.md and the README's M4 row; the links resolve. Once shesha decided
  (2026-11-05): "Decided 2026-11-05", ADR 0015 Accepted, and one-line dated pointers in ADR 0006
  (model choice and cross-model fallback to M9) and ADR 0011 (`route` stays `rag` until D6).

## PR 1 — `m4/intents-format` (kenji; zara reviews the definitions) · M · after D3, D5
New packages `intents/` and `intenteval/` [proposal]; `router.py` already means a FastAPI router.
- **Taxonomy.** `intents/taxonomy.py`: an `Intent` StrEnum with one-line definitions and a
  decision order, the first that fits wins [proposal]: `emergency`, `professional_judgement`,
  `complaint`, `refund_or_payment`, `human_request`, `action_request`, `question`, `out_of_scope`.
  `emergency` names "safety risk or distress, including thoughts of self-harm" (§3.5, §4.1).
  `out_of_scope` is decided from the text alone (not a support request to this kind of business:
  general knowledge, chit-chat); a question about a service the business may not offer is
  `question`, and RAG abstains on it (unlike `support_qa`'s document-based `out_of_scope`).
  Asking *for* a refund is `refund_or_payment`; asking *about* the policy is `question` [carried].
- **Route policy.** `intents/policy.py`, a platform module like `ingestion/language_map.py`:
  - A closed set of business types: `human_healthcare` and `veterinary` have their own templates;
    `default` covers only the listed non-health types (`ecommerce`, `retail`, `service`)
    [proposal]. An unknown or missing type raises, and `intenteval` refuses the run.
  - Platform constants, not template fields: `MANDATORY_HANDOFF` (§3.5), and `HEALTH_SAFETY` =
    {`emergency`, `professional_judgement`} for both health types (§4). Templates are tested
    against them, so a short safety list cannot pass.
  - `emergency` is urgent; `professional_judgement`, `complaint` and `refund_or_payment` high;
    `human_request` and `action_request` normal (until M5's tools); `question` and `out_of_scope`
    go to `rag`, which abstains by itself.
  - Tests: every template maps every intent; no mandatory intent maps to `rag` (tighten, never
    loosen, §2); in no template does an intent rank above one with a more urgent route; every
    manifest in `data/sample` resolves to a named, non-default template.
- **`evals/datasets/intents/v0.1/`:** a README with the definitions (a test keeps them equal to
  the module) and the content rules [D5; carried ADR 0012: everyday emergencies, no personal
  data]; a `manifest.json` (`synthetic: true`, `stage: collecting`) with D3's minimums, the
  near-duplicate threshold and the split rule, all fixed before any row.
- **The split rule** [proposal]: `split.py`'s v2 rule over intents rows. Units are split groups,
  each placed by its lead in the order author, language group, business, intent, seeded shuffle;
  50 % of eval units go to test, with the same repair (each intent in both splits). The integer
  primitives are imported; `evalset/split.py` does not change.
- **Row fields:** `id`; `business_id` (one of `data/sample`'s); `language` (`en` or `hi`, one the
  business lists) and `code_mixed`; `text` (1–500 characters); `intent`; `author` (`claude` in
  train only, `shesha` or `friend` in eval only [D3]); `verified` (train); `split` (eval, set by
  `freeze`); `split_group` (required for translations and for one message written for both
  businesses); `steering` (eval, optional); `created`; `synthetic: true`.
- **`python -m vectapilot_api.intenteval check | report | sample | freeze`:**
  - `check`: the schema, personal data (`evalset/personal_data.py`), placeholder markers, and a
    near-duplicate screen (character 3-gram Jaccard on normalised text, threshold fixed in this
    PR). A train–eval pair above it is refused; an eval–eval pair above it, or with identical
    text, must share a `split_group`. It lists pairs by id and never edits a row.
  - `report` counts per intent × business × language group × file × author; `sample` writes the
    seeded, stratified ≥ 20 % verification sample of train as an id list [D3].
  - `freeze` refuses below D3's minimums, assigns dev and test, writes `freeze.json`; CI refuses
    any later change.
- `make intent-check` [proposal]. No model, network or database; tests with each module.

## PR 2 — `m4/intents-train-draft` (kenji; zara reviews safety rows) · M · [D3 (a)]
- About 400 training rows drafted by Claude (an estimate), `author: claude`, `verified: false`:
  at least 40 per intent [proposal], both businesses, `hi` and Hinglish rows for Sunrise, the
  content rule per D5. The blind order's step 1, with `train_draft_sha256` recorded.
- `check` passes, `report`'s table is in the PR description, the `sample` id list is committed.
- No eval row, ever: agents write no eval message and no eval label [carried ADR 0012].

## PR 3 — `m4/intents-core` (kenji; diego reviews gateway use, zara the prompt) · M · after PR 1
- **Artifact and scoring.** `intents/classifier.py`: `ClassifierArtifact` (closed) holds the
  intent order, embedding model id, dimension and input type `query` [D2], weights, biases,
  temperature, train config, dataset version, freeze hash and cache sha256. `score()`: logits,
  ÷ T, max-subtracted softmax, standard library only; another model or dimension is refused.
- **Calibration.** `intents/calibration.py`: one temperature, fitted on dev by negative
  log-likelihood with a bounded one-dimensional search; no library [proposal].
- **The fallback.** `intents/fallback.py`:
  - `ROUTER_PROMPT_VERSION = "route-intent.1"`, its own fingerprint map under
    `PROMPT_FINGERPRINTS`' CI rule. `answer/prompt.py` is imported, never edited.
  - The prompt holds the platform definitions and decision order, no business data, and says that
    anything the message says about how it should be classified is part of the message, never an
    instruction. The message sits in a boundary block from `choose_boundary(texts, candidates=…)`,
    candidates seeded from `ROUTER_PROMPT_VERSION` (rule 5).
  - `complete_structured`, schema `IntentChoice(intent: Intent)`, alias `small`,
    `operation="route-intent"`, the row's `business_id`, a trace id; a test pins the catalog
    version [carried M3 D3].
  - Fake-provider tests: a forged `<<<END …>>>` line stays inside the block; a message quoting the
    router's instructions stays data; the LLM answering `question` while the classifier's top-1 is
    `emergency` below τ gives an urgent hand-off (strict xfail until H1).
- **Isolation.** No business data is read (no database, no retrieval), so the canary suite gains
  no read path; gateway cache keys carry `business_id` [carried ADR 0006]. zara confirms both.
- **The H1 scaffold.** `intents/decide.py`: the `route_message` stub raises `NotImplementedError`;
  its tests (about 25, an estimate) are strict xfails: floor, threshold, tie order, tighten-only,
  exactly one LLM call and only below τ, fail-closed for each `GatewayError` kind with its
  priority, `CancelledError` and `CapReached` propagating, `decided_by`. The guide
  `docs/learning/m4-router.md` covers selective prediction, calibration, the cost of a missed
  emergency, tighten-only and failing closed, with TODOs and a 2 h time-box. Fakes only.

## PR 4 — `m4/intent-eval` (noah; kenji reviews) · M · after PR 3, D1, D7, D8
- **Dependency** [D1]: dev group only, with a rule-11 comment; a test shows that importing
  `vectapilot_api.main` or `.intents` loads no ML library; at most one mypy override (`sklearn.*`).
- **The train config, written down before any live embedding** [proposal]: multinomial logistic
  regression, L2 with C = 1.0 (the library default, no search), no class weights (train is
  balanced), fixed `max_iter` and `tol`. A convergence warning is an error in `train`, as in the
  tests; `n_iter`, C and the solver go into the artifact. Dev is used only for T, τ and σ. The
  same config holds under D1 (a) or (b).
- **`intenteval embed | train | calibrate | thresholds | run | summarize`:**
  - `embed` is dry by default. `--live --max-usd X` (≤ $1.00, typed again at a terminal) is
    shesha's alone, through `CappedEmbedder` [carried ADR 0013 decision 4]; training rows in
    batches, eval rows one per request (own latency). Each cache entry carries the row id, the
    sha256 of the normalised text, model id, dimension and input type; `train` and `run` refuse a
    mismatch with the frozen rows. The cache is committed in the `-live-embed` folder as float32
    base64 (about 3 MB, an estimate; synthetic text only).
  - `train` fits on train only; a test checks that PR 3's scorer matches the library's
    probabilities within 1e-9 on fixed vectors [D1].
  - `calibrate` fits T on dev. `thresholds` applies D8's rule on dev (both grids fixed in this PR)
    and commits the whole dev curve, including false urgent hand-offs (non-safety rows caught by
    σ) as `x of n` at each σ [D8].
  - `run --split dev|test --mode classifier|llm`: classifier mode is local and $0; LLM mode is dry
    unless `--live --max-usd X`; test follows "Test discipline"; `--live` is refused unless every
    manifest says synthetic [carried].
  - `summarize` computes hybrid rows by calling `route_message` with a replay fallback that returns
    the LLM-only run's recorded intent or re-raises its recorded `GatewayError` subclass. Tests:
    replay and a fake-provider run decide identically; `CapReached` stops a run.
- **Folders** under `evals/reports/intents/v0.1/` [proposal]: `<UTC>-live-embed`,
  `<UTC>-live-llm-<split>`, `<UTC>-local-classifier-<split>`, `<UTC>-replay-hybrid-<split>`.
  run.json: the fingerprint (artifact sha256, T, τ, σ, `ROUTER_PROMPT_VERSION`, `catalog_version`,
  freeze hash), seed, B, status, cost. results.jsonl: ids, gold, the LLM's raw intent, final
  intent, route, priority, `decided_by`, the `GatewayError` class name, top probability, latency,
  cost; never text. The ledger `evals/reports/intents/test-runs.jsonl` keeps `ledger.py`'s append
  discipline with M4's own entry.
- **Spend** [D7]. `evalrun/spend.py`'s counting core becomes a function of (folder pattern,
  ceiling, branch, reason); M3's call and tests stay unchanged. M4's instance: D7's ceiling,
  branch `m4/close-out`, a pattern for `-live-embed` and `-live-llm-*` (committed folders only; no
  run.json counts at $1.00). A test pins that no M4 folder name matches M3's `LIVE_FOLDER`.
- **The H2 scaffold.** `intenteval/metrics.py`: stubs for `confusion_matrix` (a `fail_closed`
  column), `macro_f1` (per-class precision, recall, F1; the 0/0 rule), `reliability_bins` and
  `expected_calibration_error`; strict-xfail tests; team stand-ins in the harness's tests [carried
  M3]. Guide `docs/learning/m4-metrics.md`: macro-F1 against accuracy, ECE, counts next to rates.
- **The skeleton** of `docs/notes/m4-what-i-measured.md`, and `make intent-dry-run` [proposal].

## H1 — `m4/fallback` (`[HAND-WRITE]`, written by shesha [D4]; time-box 2 h) · S
`route_message(scores, message, policy, fallback, business_id, trace_id) -> RouteDecision`:
1. If the business type's safety mass is at least σ, return the most probable safety intent, no
   LLM call (`decided_by="safety_floor"`).
2. Otherwise, if the top probability is at least τ, return the top intent, ties in decision order
   (`"classifier"`).
3. Otherwise, call the LLM once. The final intent is whichever of the LLM's intent and the
   classifier's top-1 comes first in decision order (`"llm"` or `"classifier_kept"`): the LLM may
   add an escalation, never remove one (§2's tighten, never loosen).
4. If the LLM raises any `GatewayError`, hand off with no intent, at the classifier top-1's
   priority or `high`, whichever is more urgent (`"fail_closed"`) [proposal].
5. Anything else propagates: `CancelledError` and `CapReached` are never caught.

The route comes from the policy. The explain-back is part of acceptance.

## H2 — `m4/metrics` (`[HAND-WRITE]`, written by Claude at shesha's request [D4]; time-box 2 h) · S
The four functions from PR 4's scaffold. Counts are exact `Fraction`s; probabilities stay floats.
The explain-back is part of acceptance.
- **Built 2026-11-10** (written by Claude at shesha's request, D4): the four bodies in
  `intenteval/metrics.py`; the module's strict xfail removed, so its 21 hand-computed cases pass;
  the summary test that asserted "no summary with the real stubs" now renders with the real H2
  (the float 0.6 in [0.4, 0.6), ECE 0.400 raw and 0.100 calibrated, by hand). Bin placement uses
  `Fraction(p)`; ECE is summed exactly and rounded once. H1's xfails unchanged. Merged as #56; the
  explain-back (guide §6) is still shesha's.

## PR 5 — `m4/close-out` (shesha's live steps; kenji, noah and maya help)
Agents never pass `--live`, never run test and never see a key [carried ADR 0007]. Each run stays
within $1.00 and counts towards D7; commit each folder on `m4/close-out` before the next live run
[carried M3 PR 9].
1. **Eval rows** (blind order, step 3): shesha (and friends, optional) write and label them; then
   `check` and `report` until D3's minimums hold. `steering` rows, if any, are reported
   separately; with none, the note says "fallback injection resistance not measured live".
2. **The training sample,** after step 1 is committed: shesha verifies the ids from `sample` and
   corrects labels; the changed labels, `x of n`, go in the note.
3. **`freeze`**, then **`embed --live`** (train and eval). Commit the embed folder.
4. **`train`, `calibrate`, `thresholds`** on dev ($0). Commit the artifact and the dev curve.
5. **Dev:** `run --mode classifier --split dev`, `run --mode llm --split dev --live`, `summarize`.
6. **Test, once per mode** (shesha types the confirmation): the pinning ledger entry; `run --mode
   llm --split test --live`; `run --mode classifier --split test`; `summarize` (the comparison).
7. **Optional,** if `support_qa` v0.1 is frozen: check (classifier only, never tuned on; its
   embedding call counts towards D7) that every `safety` and `handoff_request` question hands off.
8. **The note:** every figure with dataset version, commit and config; safety misses and
   fail-closed rows by id; reruns with reasons; who saw which rows, and when; spend against D7's
   ceiling; dev labelled as tuning.
9. **The H1/H2 explain-backs,** the M4 study PDF and the quiz.
- **Done when** "How we will know M4 worked" is complete, ADR 0015 has its "Built" section, and
  CLAUDE.md says M4 is complete.

## Decided 2026-11-05 (shesha)
shesha chose the recommended option of every decision: D1 (c), D2 (a), D3 (a), D4 (b), D5 (a),
D6 (a), D7 (b), D8 (a). The options stay for the record; everything else in this file is a
proposal or carried.
- **D1 — Training dependency** (rule 11; in every option the API scores with the standard library
  and nothing reaches the image). (a) **No new package:** pure-Python gradient descent (L2 makes
  it strictly convex), about 80 more lines, minutes to train (an estimate). (b) **numpy only**
  (dev group): no dependencies of its own, but we still own the optimiser. (c) **scikit-learn**
  (dev group; the scikit-learn project, BSD-3-Clause; pulls in numpy, scipy, joblib and
  threadpoolctl, to confirm with `uv lock`; about 150 MB, an estimate): a standard L-BFGS fit in
  a few lines and an independent reference for tests. **Decided: (c):** least code to defend.
- **D2 — Embeddings for the classifier.** (a) **voyage-4 query embeddings** (ADR 0008), from
  shesha's live `embed` step; once wired, retrieval's vector is reused, so no second call.
  (b) **A local multilingual model:** $0, but torch or onnxruntime plus hundreds of MB, on an 8 GB
  Mac with 8.4 GiB free. (c) **Character n-gram TF-IDF:** $0, but not the playbook's sentence
  embeddings, and weaker across scripts (expected, unmeasured). **Decided: (a).**
- **D3 — Dataset authorship and size.** (a) **Claude drafts about 400 training rows; humans write
  the eval rows,** in the blind order; shesha verifies at least 20 % of train (seeded, stratified)
  after writing every eval row. At least 160 eval rows, 50/50 dev/test: at least 20 per intent;
  per business at least 10 `emergency` and 10 `professional_judgement`; for Sunrise at least 6
  safety rows in Hindi or Hinglish, 3 of them `emergency`. (b) **All human-written,** at least 500
  rows, 60/20/20: faithful to the playbook, but days of writing. (c) **Claude drafts both,**
  shesha verifies 100 % of eval: fastest, but one author for both flatters the numbers, against
  ADR 0012. **Decided: (a):** no leak by authorship or order; measured on human messages.
- **D4 — Who writes H1 (fallback) and H2 (metrics).** (a) shesha writes both. (b) shesha writes
  H1; Claude writes H2 at shesha's request. (c) Claude writes both at shesha's request, as in M3.
  Explain-backs stay in every option. **Decided: (b):** H1 is M4's interview story ("when not
  to use an LLM") in about 40 lines (an estimate); H2 repeats M2's metric pattern. This is rule
  2's one reminder of the learning goal.
- **D5 — Self-harm messages in M4's data** (the `emergency` definition names them in every
  option). (a) **None in M4:** LLM-band eval rows go to the provider live, where such messages may
  fall under its 2-year flagged retention (ADR 0007); the note says safety recall excludes crisis
  messages (M7). (b) **A few by shesha, classifier only:** 2+ per business, never in an LLM run.
  (c) **A few by shesha, everywhere,** the live LLM runs included. **Decided: (a).**
- **D6 — Wiring into `POST /v1/answer`.** (a) **Not in M4;** wiring comes with M5's tool route.
  (b) **Behind `router_mode`** (default `off`): a new response and record contract. (c) **On at
  M4's close:** changes `support_qa` outcomes, so only after M3's last live answer run.
  **Decided: (a):** M3's measurement window and M2/M3's baselines stay intact.
- **D7 — M4's spend ceiling** (each run ≤ $1.00 in every option). (a) $1.00 in total, no rerun.
  (b) **$2.00 in total.** (c) $5.00, as M3. **Decided: (b):** one rerun's room; with M3's
  $5.00, $3.00 of the $10/month workspace remains for M2 in one month (Voyage bills separately).
- **D8 — Threshold protocol** (σ: the safety floor; τ: the confidence threshold), fixed before any
  live result. (a) **Fixed grids and a written rule, on dev** ($0; grids fixed in PR 4, σ's down
  to 0.01). τ first, before σ exists: the smallest grid value at which the classifier alone is
  right on at least 95 % of the dev rows whose top probability reaches it, with at least 20 such
  rows, and at every larger grid value that still keeps at least 20 rows (reading (C), decided
  2026-11-09); if none, there is no τ: **no confidence band**, everything below the floor goes to
  the LLM (decided 2026-11-09, replacing "τ = 1.00"). Then σ, given τ: the largest grid value at
  which the classifier alone decides no dev safety row as a non-safety intent, one grid step lower
  as a margin; if none qualifies, 0.01.
  Dev safety rows still missed (also an emergency the floor decides as `professional_judgement`,
  which σ cannot change) are listed by id; the dev curve shows false urgent hand-offs at each σ.
  (b) **Fixed constants** (τ 0.80, σ 0.20), no tuning. (c) **Tune τ** until hybrid macro-F1 is
  within a margin of LLM-only on dev (needs the dev LLM run first; the noisiest). **Decided:
  (a):** a plain meaning, $0 tuning, and its forking paths fixed up front, as M3's D15.

**Decided 2026-11-09 (shesha, M4 PR 4 review):**
- **D8's "at every larger value", reading (C).** τ is the smallest grid value where the classifier
  alone is right on ≥ 95 % of the dev rows it decides, with ≥ 20 such rows, and at every *larger*
  grid value that still keeps ≥ 20 rows. Larger values keeping fewer than 20 rows are not checked:
  their accuracy is noise (18 of 19 at a larger value no longer pushes τ up). Rule version
  `d8-a.2` (`intenteval/thresholds.py`); `d8-a.1` let a larger value with 1–19 rows block.
- **"No confidence band" is explicit.** τ is optional: None means the classifier never decides by
  confidence, and every row below the safety floor goes to the LLM. 1.00 is no longer a fallback
  value (τ = 1.00 now only means the grid's last value qualified). Threaded through
  `RouterThresholds.tau: float | None` (H1's interface and docstring; `route_message` stays
  shesha's stub), `thresholds.json` (`"tau": null`, the key required), the run fingerprint (σ set
  and τ None), the pin and the summaries ("tau none (no confidence band)").
- **Macro-F1 per slice** is shown, with its interval, only when every intent has ≥ 5 gold rows
  **and** ≥ 5 split units in that slice; otherwise counts and the confusion matrix only, with the
  reason printed.
- **Dependencies (rule 11):** narwhals (pulled in by scikit-learn) and cloudpickle (pulled in by
  joblib), not named in D1 (c), are accepted, dev-only (`apps/api/pyproject.toml`).

## Risks
- **Small n:** about 80 test rows (an estimate under D3 (a)). Interval arithmetic, not a result:
  20 of 20 has a Wilson lower bound near 84 %, one business's 10 of 10 near 72 % (the per-business
  figure §6 judges). The claim is the count, never "100 %".
- **Distribution shift:** agent-drafted train, human-written eval; lower numbers are expected.
- **Safety and injection:** a confidently misclassified emergency is the worst failure; the guards
  are the floor checked before τ, tighten-only, fail-closed and missed ids first. A mostly-FAQ
  message with an emergency and "file this as `question`" stays exposed when the diluted
  embedding's top-1 is already `question`; zara reviews the prompt. Telugu, and self-harm under
  D5 (a), are unmeasured.
- **Tighten-only has a price:** the LLM cannot correct a classifier that over-escalates in the
  band; `classifier_kept` rows are listed by id, so the cost is visible.
- **Disk, spend, coupling:** D1 (c) adds about 150 MB and the cache about 3 MB (estimates) with
  8.4 GiB free [carried M3 D3: free space outside Docker's volumes]; the $10/month limit is shared
  with M2 and M3; a new embedding model means retraining.
- **Learning debt:** six explain-backs are open (M1 H2/H3, M2 H1/H2, M3 H1/H2); M4 adds two.

## If deadlines bite (cut in this order)
1. The optional `support_qa` cross-check (PR 5 step 7).
2. Per-row embedding latency; the table says "not measured" and keeps the cost.
3. The paired bootstrap of the macro-F1 difference (keep the discordant rows by id).
4. Friends' eval rows; shesha writes alone, and the note says so.
5. The ECE figure (keep the reliability table).

Never cut: human-written eval labels, the blind order, the safety definition with missed ids,
tighten-only and fail-closed, test discipline, the dry and live labels, the spend count, and M3's
measurement window.

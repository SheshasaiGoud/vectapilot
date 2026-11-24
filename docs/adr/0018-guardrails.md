# ADR 0018 — Guardrails: an emergency screen first, guards that wrap and only tighten, masking before third parties, a frozen red-team suite in CI

- **Status:** accepted · 2026-11-16 (D1–D8 decided by shesha, each as recommended, with every
  sub-choice: `docs/plans/m7-pr-checks.md`, "Decided 2026-11-16 (shesha)"). Drafted 2026-11-15
  by maya-tech-lead from zara-safety's targets (`.claude/agents/zara-safety.md` and her read-only
  inventory at 313a8e8), reviewed read-only the same day by zara-safety, noah-evals,
  diego-backend and leo-agents (findings applied; adaptations and rejections in the plan's
  "Review notes (2026-11-15)"), rewritten 2026-11-16 to match the revised plan. Parts marked
  "Dn" follow that decision. G1–G15 are that plan's guard rules. Nothing is built yet.

**Context.** The README names M7 "Safety — PII masking, injection defense, red-team suite".
zara's targets add moderation, emergency-keyword escalation, rate limits, per-user budgets and 50+
adversarial cases in CI with honest gaps. Business-context §3.5's proposed platform default gives
"emergency, safety risk or distress" an urgent hand-off with guidance and no AI advice, on every
business type; §4 adds the health boundaries (no diagnosis, no dosing, red flags per type,
professionally reviewed: Q8). §6 makes safety-escalation recall a 100 % hard gate "gated from M7".
Rule 5: retrieved text, documents and tool results are data, never instructions. Rule 9: nothing
business-specific in code, every path scoped by `business_id`.

Already in place (zara's inventory): metadata-only records, prompt boundaries that cannot be
forged, citations verified by id, a loop that cannot write and a confirm token no model sees (ADR
0016), W11 failing health businesses closed on `/v1/agent` while the router is off, the canary
gate. Missing (read from the code, not run): `/v1/answer` answers chest pain with a fee question
(`answer/service.py:151`); with the router off, an emergency on `/v1/agent` is a *normal*
`safety_floor_off` hand-off (`agent/service.py:258-262`), and other types get no safety step;
nothing masks personal data before Voyage (`embeddings/voyage.py:160`), Anthropic or the logs
(`log.py:25-28`); replies go out verbatim; invisible Unicode passes `check_question`; no limits
or budgets exist (`config.py:79-80`).

Eight constraints shape the choice:
1. **M3's measurement window** freezes `gateway/models.toml`, `PROMPT_VERSION`,
   `answer/prompt.py`, the gateway's code and output path, and `POST /v1/answer`'s responses.
2. **M4–M6's pending runs go through the services.** `tooleval` drives
   `AgentService.respond_unrouted` and imports `main`; `evalrun` builds `AnswerService`;
   `extracteval` and `intenteval` import routing code. shesha's three H1 stubs live in
   `agent/loop.py`, `intents/decide.py` and `extraction/routing.py`.
3. **Nothing faces a customer yet** (`X-Business-Id` only in local, test and ci). Both sample
   businesses are health types and the router is off, so W11 hands off every `/v1/agent` message
   before the loop; M5's `run_turn` is a stub; no `search_kb` tool exists.
4. **Debt and open questions.** Three H1s are unwritten, nine explain-backs are open, and
   guardrail checks are `[HAND-WRITE]` (rule 2). No moderation service before Q4c, no
   professional term review (Q8), no Telugu reviewer, no package without rule 11.
5. **Disk** is 95 % used (11 GiB free): no new image or service. Redis is in compose, CI and
   `uv.lock`, without a volume; redis-py waits 5 s by default.
6. **Isolation** (ADR 0004, 0009): new stores join the canary suite.
7. **Seams.** The services take concrete classes (`LLMGateway`, `SearchService`, `ToolExecutor`);
   a FastAPI dependency can only raise; a middleware also runs on `/healthz`.
8. **Parts to reuse:** the invisible-character predicate (`tools/config.py:79-98`),
   `request_reservation`, the `CappedGateway` pattern, the canary gate script, `wilson_interval`,
   the ledger and `SpendRules`.

**Options.** D1–D8 are open; each "proposal" is the plan's recommendation.
- Masking (D1): the standard library with checksums; plus `phonenumbers`; Presidio (proposal: (a)).
- Placement (D2): on for `/v1/agent`, its confirm route and `/v1/extractions`, built but off on
  `/v1/answer` and `/v1/kb/search`; those three only; everywhere now (proposal: (a)). Moderation:
  a small `abuse` output-phrase category; no check (proposal: (i)).
- Failure (D3): all closed; the limiter open when Redis is down; all open with an alert
  (proposal: (a)). Uncovered languages: hand off; answer unscreened (proposal: (i)). Guard reasons
  on `/v1/agent`: new `HandoffReason` values; all `agent_error` (proposal: (i)).
- Limit store (D4): Redis; Postgres under RLS; in-process (proposal: (a)).
- Hand-writes (D5): shesha H1 and H2; shesha H1, Claude H2 and H3; Claude all (proposal: (b)).
- Red-team (D6): about 140 agent-drafted cases frozen before the terms plus a held-out probe; ≥ 50
  by shesha; agents only (proposal: (a)). Probe: new cases; M4's blind eval rows; both
  (proposal: (iii)).
- Live spend (D7): $0; ≤ $1.00 for one raw-arm run; $2.00 (proposal: (b)).
- CI (D8): a gate script in the `api` job; a separate required job; report only (proposal: (a)).
  Close-out: allow `pending:M5-H1`; wait for M5's H1 (proposal: (i)).
- Architecture (team proposal): guards inside the services; a middleware or dependencies;
  explicit calls at the routes plus subclass seams built in `create_app` (proposal: the last).

**Decision.** A guard pipeline that only tightens, called explicitly at the routes and through
subclass seams built in `create_app`, around services that stay unchanged. The emergency screen
runs first and depends on nothing that can fail. Each part:
1. **Order and the emergency screen (G1, G2).**
   - Message routes: clean and fold → red-flag screen → limits and budget → injection signals →
     mask → service → output checks. Validation runs first, so an over-length or NUL emergency
     gets a 422 (a known gap until M8's widget). Search, confirm and `/v1/extractions` have their
     own order (parts 3, 8, 10).
   - The red-flag screen is pure and in-process, with no model or Redis call. A hit ends the
     request: an urgent (`emergency`) or high (`professional_judgement`) hand-off with the
     business's guidance, the most urgent hit winning, priorities read from M4's route policy. On
     `/v1/agent` it runs before W11 and the router, so a red flag is urgent where today it is
     normal, and non-health types get a deterministic step.
   - A guard may only hand off, raise a priority, remove write tools, drop a chunk, withhold text
     or replace a whole response. It never edits a `confirmation_required` response (W3). W11
     stays.
2. **Fail closed into a human (G3; D3 (a)).**
   - A guard's exception or a Redis or model timeout is a `guard_error` hand-off, never a 500.
     Priority mirrors `_router_failed`: before the red-flag screen has cleared the message, urgent
     for a health business and high otherwise; after, high and normal. Every refusal for a health
     business carries the guidance. Search returns a typed 503.
   - Synchronous work is bounded by input caps per surface and a pattern rule, not by timeouts.
     `CancelledError` propagates; errors are raised outside `except`.
   - D3 (i): `guard_error`, `guard_output`, `budget`, `guards_not_configured` and
     `not_covered_language` join `HandoffReason` and `tooleval`'s infrastructure set (one line);
     a red flag reuses `safety`. A health business's message in a `not_covered` language's script
     hands off at high priority.
3. **Wrap, never edit (G13), and M3's window (G11; D2 (a)).**
   - No middleware and no screening dependency: a dependency only hands the route its guard
     object, and the route calls it in G1's order inside one `except Exception`. The health
     probes and the docs never pass a guard; `/readyz` is unchanged.
   - Seams are subclasses, as `CappedGateway` is: `BudgetedGateway`, a screening `ToolExecutor`,
     a chunk-screening search, each tested to override every I/O method its service calls.
     Read-only turns run on a second `AgentService` whose profiles are re-validated with
     `READ_TOOLS` only. The masked message is re-validated as an `AgentRequest`.
   - Unchanged by every M7 PR but PR 11: `answer/service.py`, `answer/prompt.py`,
     `AnswerResponse`, `agent/service.py`, `agent/loop.py`, `agent/llm.py`, `intents/`,
     `gateway/`, `extraction/` but its route, and the four harnesses (`tooleval` but D3 (i)'s
     line). The fence: an AST import check, construction tests of what `tooleval` and `evalrun`
     build, their unchanged tests, an empty `git diff --stat`, and a pin of
     `build_action_services`'s source.
   - `guard_routes = on` (`/v1/agent`, confirm, `/v1/extractions`), refused `off` outside local,
     test and ci. `guard_answer = off` (`/v1/answer`, `/v1/kb/search`), byte-identical while off
     (patched trace ids, a closed list of volatile fields, a dead Redis, no `guard` record). PR 10
     turns it on after shesha's last M3 live answer run; production then refuses `off`.
   - When on, a red flag on `/v1/answer` returns `AnswerSafetyHandoff` (priority, intent,
     guidance) in a response union: `AnswerResponse` keeps its bytes, and the OpenAPI schema
     gains one shape even while off. On search, which returns no model text, a hit is only
     recorded.
4. **Configuration, one normaliser, one matcher, a self-test (G4, G5, G14; H1).**
   - `data/safety/` (a read-only mount): `platform.json` (non-health emergency and distress terms,
     injection and output phrases, script ranges); `types/<type>.json` per health type (terms per
     language mapped to M4's intents, `not_covered_categories: ["self_harm"]`); and
     `businesses/<id>.json`, which only adds terms, carries `synthetic: true` and a `not_covered`
     language list, and is the guidance source: lines verbatim from the business's
     `emergency.md`, tested equal to `tools.json`'s.
   - Terms are `phrase` or `near` (word sets within a window, any order) and cite a §4.1 or §4.2
     line or a synonym rule. A business whose type is unresolved gets every type's terms.
   - Startup refuses (among G4's checks) an unknown type, category or intent, a loosening file, a
     manifest language neither covered nor `not_covered`, a guarded health business without
     guidance, and a file without `synthetic: true`. The folder is required in `create_app` only:
     when a guarded route serves businesses without it, production refuses to start, and local,
     test and ci hand off (`guards_not_configured`).
   - *Clean* (what the model, the embedder and masking see) drops invisible characters and
     variation selectors but keeps ZWNJ, ZWJ, VS15 and VS16, applies NFC and collapses whitespace.
     *Fold* (what screens and terms see) also drops those, strips Latin, Greek and Cyrillic
     accents, case-folds, maps confusables and turns punctuation into spaces. Devanagari and
     Telugu signs stay, except that (**amended 2026-11-16 (shesha)**, D-hindi-fold (a)) Devanagari
     chandrabindu folds to anusvara and the nukta is dropped, for terms and text alike, so the
     everyday spellings साँस/सांस and तकलीफ़/तकलीफ match; Telugu's equivalents wait for a Telugu
     reviewer. Tag characters, VS17–256 and runs of three or more invisible characters are an
     injection signal.
   - One pure matcher (H1, `[HAND-WRITE]`, D5 (b): shesha) serves red-flag, injection
     and output phrases: literal terms, whole words, `\s*` between words, `near` windows; ids and
     categories out, never text. The fold stays team code.
   - G14: at startup and in CI, every term must match itself and its obfuscated variants, and
     fixed negatives must not. A miss stops startup.
5. **Masking before every third party and every log (G6, G10; D1 (a); H2).**
   - The standard library: patterns per locale over Unicode digits; typed, non-reversible
     placeholders (`[PHONE]`, `[EMAIL]`, `[AADHAAR]`, `[PAN]`, `[CARD]`, `[UPI]`, `[IFSC]`,
     `[PASSPORT]`, `[ID_NUMBER]`). Shape decides: any Aadhaar-shaped or 13–19 digit run is masked;
     Verhoeff or Luhn only picks the label.
   - Masked: the message, free-text tool-result fields (never ids or numbers), and the question
     before Voyage and the model behind `guard_answer`. Not extraction, whose values are the
     identifiers.
   - A `RedactingJsonFormatter` installed only by `create_app` masks each formatted line,
     `exception` included (**amended 2026-11-18 (shesha)**: installed from M7 PR 3; masking
     switches on by itself once H2 exists, and every logger's exceptions are class names and
     frames only, never their message).
     `JsonFormatter` and the `llm_call` lines stay byte-identical (the M1 CLI, the MCP server,
     M3's window).
6. **Injection signals that tighten (G7).** A message hit runs the turn read-only. A chunk hit
   drops the chunk (reachable only under `guard_answer = on` or `agent_router = on`). A
   tool-result field hit becomes `[withheld]`. A hit in `create_ticket`'s summary replaces the
   response with a `guard_output` hand-off, returns no token and expires the stored pending row at once
   (**amended 2026-11-17 (shesha)**, M7 PR 2). A document hit is flagged (part
   10). Encoded payloads are a known gap; boundaries, citation ids and W1/W2 stay the real line.
7. **Output checks (G8; H3, D5 (b): Claude at shesha's request).** On replies, answer
   text and ticket text, never on a hand-off or a confirmation: success claims; medical advice
   and dosing on every type, diagnosis on health types; PII not in the message, a cited chunk or
   the guidance (tool results never exempt); boundary echoes; digits no cited chunk contains;
   abuse terms under D2 (i). A hit is a fixed `guard_output` hand-off. No guard reads the
   database.
8. **Limits and budgets (G9, G12, G15; D4 (a)).**
   - Keys: `vp:{business_id}:rl:{route_slug}:{all|h}` and `vp:{business_id}:budget:{yyyymmdd}`,
     where `h` is SHA-256 over `business_id:customer_ref`, cut to 16 hex: pseudonymous, never
     called anonymous.
   - A per-business ceiling (the real control) and advisory per-customer limits, one atomic
     script per request; a limit is a 429 with `Retry-After`.
   - A daily per-business budget in integer micro-USD. `BudgetedGateway` subclasses
     `LLMGateway`, reserves the worst case before each paid call, settles from the result's cost
     (an error keeps the reservation), and wraps every gateway a guarded route reaches. A refusal
     raises `GuardRefused` through the service's `propagate` and becomes a `budget` hand-off.
   - Every Redis call has a short timeout; every limit is an unmeasured placeholder.
     `api_allow_paid_llm` stays: embeddings, and the answer path while off, are unbudgeted.
   - Confirm (G15): a limiter pre-check (a 429, or a 503 `limits_unavailable`) before
     `ConfirmService.confirm`, with nothing wrapped around the call, so a 429 never follows a
     write. `/v1/extractions` checks limits before reading the body.
   - Canaries: A's exhaustion leaves B untouched; every `vp:` key has a business segment; the
     catalog check M3 deferred lands.
9. **Records (G10).** One metadata-only `guard` record per request (`decided_by`, counts,
   `error_type`, budget amounts, durations): never text, a term or a term id. A global log canary
   with forced exceptions covers every route and MCP `tools/call`.
10. **No migration in M7 (PR 7, PR 11).** Page text is screened after extraction, before storing;
    hits go to the `guard` record and an unstored `document_flags` field. Forced review needs a
    `Reason` in the fenced `extraction/routing.py` and migration 0008, so it waits for PR 11
    after M6's runs. A KB scan CLI under RLS prints chunk ids and codes only.
11. **The red-team suite and CI (D6 (a), D7 (b), D8 (a)).**
    - `evals/datasets/redteam/v0.1/`: about 140 synthetic cases in categories A–L, frozen before
      the term files. G holds 30 red flags (15 per business), split by obfuscation class into
      G-in (hard) and G-out (`known_gap`). No self-harm content, no Telugu case before a
      reviewer exists, and PII only as placeholders filled at run time.
    - Labels in `gates.json` only tighten; a wrong case leaves only through v0.2. A case passes
      only if its outcome *and* `decided_by` match, with positive controls, so neither
      `guard_error` nor a stub can pass.
    - Loop cases run on `rt-shop`, a test-only `ecommerce` fixture, through a replay stand-in
      (frozen steps, not a loop) until M5's H1.
    - `apps/api/scripts/redteam_gate.py` in the `api` job checks every frozen id once, closed
      reasons and the soft ceiling; from PR 9, `--final` allows only `pending:M5-H1` (proposed
      D8 (i)).
    - The gate claims exactly "100 % of the frozen `hard` emergency and professional-judgement
      cases (G-in and H-req) of redteam v0.1; self-harm excluded by decision", as `k of n` with
      Wilson. Over-escalation is counted on benign look-alikes.
    - The held-out probe (D6 (iii)) and the ledgered raw-arm live run (≤ $1.00) are
      shesha's alone and reported apart; no LLM judge.
12. **Not in M7.** A moderation API, NER, `phonenumbers`, an LLM judge; authentication,
    per-session, edge or IP limits; per-user budgets; masking at rest; crisis content by agents;
    guards in the MCP server; budgets for embeddings; lifting W11; the answer path on inside the
    window; a migration; a `/readyz` change.

**Alternatives rejected** (the plan's G13 and "Review notes").
- *Guards inside the services:* `tooleval` and `evalrun` drive them, so M4–M6's runs would
  measure different code.
- *A middleware:* it runs on the health probes (a Redis outage would fail liveness) and cannot
  return each route's typed hand-off. *A screening dependency:* it can only raise, so an
  emergency would get a 429.
- *A `logging.Filter`, or redaction from PR 1:* `JsonFormatter` builds `exception` after every
  filter has run, and a stub would blank every line.
- *Hash pins of the fenced files:* shesha's H1 stubs live there, so her first merge would fail an
  M7 test. *A runtime `sys.modules` fence:* `tooleval` imports `main`; what it constructs matters.
- *Guidance only from `ToolsConfig`:* `tools_config_dir` is optional, and moving the lines edits
  M5's profile path during its pending runs.
- *HMAC keys:* a new secret buys little for a client-asserted synthetic slug that M8 replaces.
- *A parameter on `build_agent_gateway`:* it edits `agent/llm.py`.
- *A recorded loosening path:* one way out (a new version) keeps hard gates from shrinking.
- *An app-wide 422 screen, a base64 decoder, Telugu cases:* listed as known gaps instead.

**Consequences.**
- **An emergency never waits on Redis, the budget, the model or a guard** (each a test). It still
  needs the API process and Postgres (the business lookup) until M8.
- **The safety gate is narrow and worded exactly.** Phrase lists miss paraphrases, typos and
  transliteration (G-out, by id), and 30 of 30 still allows about 11 % misses (arithmetic,
  Wilson). The term author can read the frozen cases, so the probe is the honest number. The
  lists are public, so the screen's value is recall and fail closed, not secrecy. M4's router
  stays the semantic layer.
- **Over-escalation is the price of recall.** "Chest X-ray fee?" may go urgent, and under D3 (i)
  every Telugu message to a health business hands off. Precision waits for M8's staff labels.
- **M2–M6's pending measurements stay valid.** The cost is a second record: `agent_turn` shows the
  loop's outcome, `guard` what the customer got.
- **The default configuration never reaches the agent's model**, so agent-side guards are tested
  on `rt-shop` and in router-on tests. Chunk screening has no default-on path until PR 10 or M4's
  router, and `/v1/answer` stays unguarded by default until PR 10.
- **Masking is pattern-based.** Names and addresses still leak, a callback number becomes
  `[PHONE]`, long order ids are masked, and Q4c stays open: health text is still health data.
- **Fail closed is visible:** while H1 or H2 is a stub, a guarded `/v1/agent` hands off every
  message.
- **Limits are weak until M8.** `customer_ref` is client-asserted; red-flag hits skip the limiter
  and can flood the urgent queue; rotating refs can spend a business's daily budget; a Redis
  recreate zeroes the counters and the day's budget.
- **Documents are flagged, not routed,** until PR 11. Hidden text beyond `3 Tr`, white fill and
  off-page is a known gap.
- **New isolation surfaces:** Redis keys and the KB scan join the canary suite.
- **Debt grows** by the hand-writes, three explain-backs, a term review and the probe.

**Revisit when:**
- M3's last live answer run is done: PR 10 turns `guard_answer` on; re-run the suite.
- M6's live runs are done: PR 11 (forced review, migration 0008); D's cases become hard.
- M5's H1 lands: the real-loop pass, then D7's live run (with M2 step 11's key).
- M4's router goes live: compare its safety floor with the screen on M4's test rows under D6 (ii)
  or (iii), not on the frozen suite; decide on W11, never on the phrase list alone.
- Q8 is answered: professional review of every term list; crisis terms and cases (shesha's). A
  Telugu reviewer exists: Telugu terms and cases. Q4b names countries: `phonenumbers` (D1 (b)).
- Q4c is answered: real data and a moderation service. M9: masking at rest.
- M8 authenticates customers: per-session limits and budgets, contact details from the session,
  staff labels for precision, the widget's length limit.
- M10 picks a load balancer and a regression gate: Redis as a readiness dependency, edge limits,
  a separate red-team job (D8 (b)).
- Any hard red-team case fails, or a real miss is reported: stop and fix the screen first.

**Amended 2026-11-24 (shesha, M8 plan; [ADR 0019](0019-sessions-staff-access-and-ui-hosting.md)).**
"M8 authenticates customers": the widget routes call the same `agent_guard` and confirm limits as
`/v1/agent` with no unguarded branch (G13 holds), the per-customer window becomes per session
with no limiter change, the widget enforces the length limit in code points, staff labels are
stored for precision (computed in M11), and contact details stay out until Q4c. The frozen agent
and confirm cases replay through the widget routes (a parity gate). PR 11's migration takes the
next free number (M8 plan P4).

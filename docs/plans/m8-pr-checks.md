# M8 — acceptance checks per PR

> **Status: planned 2026-11-24, decided 2026-11-24 (D1–D15 by shesha, each as recommended; D8
> and D14 answered separately: "Decided 2026-11-24 (shesha)" below).**
> Drafted by maya on `m8/plan` in lean mode (one builder and one combined review per PR; routine
> choices are proposals that the team applies unless that review changes them), from
> sofia-frontend's and diego-backend's read-only targets of 2026-11-24 at bbdf5c6 (round 1 of
> "Review notes"). Reviewed read-only the same day by zara-safety, noah-evals and leo-agents
> (round 2): every finding is applied, and the six that needed shesha were D6 (sharpened) and
> D11–D15. [ADR 0019](../adr/0019-sessions-staff-access-and-ui-hosting.md) is accepted
> (2026-11-24).
> M1–M7's live runs and shesha's H1s are pending. M8 does not wait for them: every PR is built and tested at $0
> (fake provider, M7's stand-in matchers, fixtures). M8 has no paid step.

"PR 0–6" are plan step numbers, not GitHub pull-request numbers. Each PR copies its section into
its own acceptance check. Tags: **[Dn]** = follows decision Dn (decided 2026-11-24 as
recommended) · **[proposal]** = routine, applied unless its PR's review changes it ·
**[carried]** = from the named earlier plan, ADR or business-context section. Lifetimes, limits, intervals and sizes are configuration or
estimates, never measurements. No number in this file is a result (rule 3); arithmetic is
labelled as arithmetic.

## What M8 delivers
The roadmap's "Staff dashboard — review queue, decisions, corrections become evals", plus the
customer chat widget (sofia's brief: citation chips, quick replies, feedback, "talk to a human")
and the server-side sessions that both need. M8 is the first milestone that can face a customer:
today no business route can be served in production (`business.py:24,38`).
1. **Server-side sessions per business** [D1, D3, D4, D5]. Business, customer and staff identity
   come from a session row looked up under RLS, never from a header or a body field. They replace
   `X-Business-Id` ("until M8", ADR 0009) and the client-asserted `customer_ref` (ADR 0016
   decision 9, M7's G9) on every customer and staff surface.
2. **A hand-off store** [D6]. Hand-offs are returned today but stored nowhere. M8 stores them
   (masked text, guidance shown, reason, origin, priority; one open row per session, S17) so a
   person can act on them, and under D6 (a) one metadata-only row per turn, with no text.
3. **The customer widget** [D2, D3, D9]. Every state is first-class: greeting with the AI
   disclosure, working, answer (only with sources), reply, confirmation, hand-off (guidance
   first), rate limit, error and session expired. It is honest about hand-offs, which is all the
   real stack can show today.
4. **The staff dashboard** [D4, D7]. A queue sorted urgent → high → normal, a detail view with
   the guidance the customer saw and that session's pending actions (read-only), one-click
   claim and resolve, and the escalation label §6 needs ("staff agree it was needed").
5. **Extraction review decisions.** Per-field accept, correct or reject for M6's review queue,
   in a new append-only table (the app role cannot UPDATE `extraction_fields`, migration
   0007:37-38).
6. **Isolation extended to every new store and route.** The canary suite stays the hard gate:
   zero cross-business leaks, plus same-business session-to-session checks.
7. **Corrections and labels as eval candidates.** An owner CLI exports them into the
   git-ignored `evals/reports/tmp/`, never into a frozen dataset; shesha decides what enters one.

## How we will know M8 worked (PR 6)
- **Isolation, in CI ($0):** `scripts/canary_gate.py` passes with the new tables (picked up from
  `pg_catalog`) and every new route in `READ_PATHS`, `WRITE_PATHS` and `OWNER_CONNECTION_PATHS`.
  The session attack tests (token-prefix tampering, a planted shared uuid, session X against
  session Y in one business, header or body smuggling, CSRF, framing headers) live under
  `tests/canary/`, so the canary gate's no-skip rule covers them. Zero leaks.
- **No client-asserted identity in production:** a route-inventory test under production
  settings finds no reachable route that depends on `get_business_id`; every `/v1/staff/*` route
  mapped to exactly one role, and every non-GET one carrying the Origin dependency (S5); every
  customer route behind a session; no widget route with an unguarded branch (S6); no
  `/v1/staff/*` route that depends on the confirm service or can serialize `ConfirmationResponse`.
- **Safety carries over (S7):** the frozen red-team cases of the agent route (100) and the
  confirm route (2) (zara's count by route, 2026-11-24) replay through the widget routes with the
  same kind, reason, priority, guidance and `decided_by` as through `/v1/agent`. Two runs: with
  M7's test-only stand-in matchers (`tests/redteam/standins.py`, the run that means something
  while H1 is a stub) and with the registered seams. Reported as "k of K identical, j excluded
  (ids and reasons)", with the outcome distribution (how many are not `guard_error`) and the
  suite's fingerprint. `scripts/parity_gate.py` reads pytest's JUnit report and fails on a
  missing, doubled or skipped id. That is how §6's 100 % safety-escalation recall gate reaches
  the widget.
- **PII at rest:** M7's 18 category E cases and F's echoed-PII case, replayed through the widget
  route, leave no `redteam.pii.leak_forms` value in `handoffs.message_masked` or in the export:
  reported as "18 of 18" inside the parity gate (S11).
- **Honest hand-offs:** "a person will reply" appears only when the hand-off row exists and the
  hand-off is not urgent (tested both ways, D11); guidance is first and verbatim, and also shown
  in every non-success state of the widget (S16); a failed store is never a 5xx and never drops
  the guidance; with Redis down, an emergency on an existing session still gets an urgent
  hand-off; a flood on one session gives one queue row (S17).
- **Both UIs:** every state has a fixture that validates against the API's pydantic models, a
  unit test of the state logic and, under D8 (a), a browser test with axe (tags `wcag2a`,
  `wcag2aa`, `wcag21a`, `wcag21aa`, `wcag22aa`; zero violations of any impact; every disabled rule
  listed with its reason; retries 0; any request that is not stubbed fails the test). The manual
  pass (keyboard only, VoiceOver on macOS Safari, an iPhone-width VoiceOver pass, 200 % zoom,
  320 px reflow, reduced motion) is recorded with date and versions. Automated results are a
  floor, never a conformance claim.
- **Real stack, locally:** a session, a message that hands off, the hand-off in that business's
  queue and not in the other's, claim, resolve with a label. Then the database goes back to 0005.
- **Earlier milestones untouched:** each PR shows an empty `git diff --stat main...HEAD` on the
  fenced paths (S13), `/v1/answer`'s golden test passes unchanged, and `make mcp-check` passes.
- **Measured in M8, and only this:** the gzipped JavaScript of the widget page and of the
  team-site home page, each on `main` before PR 4 and after it, with commit, `next` version and
  build command (M0's method); test counts; the parity and PII counts above. Nothing else in M8
  is a result.
- **§6's metrics in M8** (the note repeats this table; "no" means no number is reported):

  | §6 metric | Measured in M8 | Data M8 collects | What blocks it | First milestone |
  |---|---|---|---|---|
  | Cross-business leaks (gate) | yes, canary suite ($0) | none new | — | M2, extended |
  | Safety-escalation recall (gate) | on the frozen suite through parity, not on traffic | none new | real traffic | M7 suite; M11 traffic |
  | Escalation precision | no | `handoffs.staff_label`, `origin`, `reason` | synthetic labels measure nothing (D10) | M11 |
  | Escalation recall in production | no | none: labels on hand-offs show only false positives; a missed escalation inside an answered turn is never stored | answered turns reviewed | M11 |
  | Wrong-answer rate | no | none (D14 (a)) | no answered turn on the real stack (router off, H1s) | M11 (D14) |
  | Auto-resolution | no | hand-offs, 👎, `widget_turns` (D6) | "repeat within 24 h" with anonymous sessions (D13) | M11 |
  | Thumbs-up rate | no | `turn_feedback` bound to `widget_turns` (D6) | the real stack shows only hand-offs, so no rating control appears | M11 |
  | Web-chat latency p95 < 6 s | no | `widget_turns.route_latency_ms` (D6) | no real answers; per-conversation traces | M9 |
  | Cost per conversation | no | `trace_id` per turn and session (D6) | tracing | M9 |
  | Correctness, faithfulness, hit@5 | no | none | shesha's live runs | M2–M3 |

  Every session, hand-off and turn row carries `business_id`, `channel` and `ui_language` (the
  UI language at session start, never the detected message language), so §6's breakdown by
  business type, language and channel can be done later; it cannot be backfilled.
- shesha's explain-backs (S3, S5, S14, S16, S17, D2's same-origin argument and D12) and the M8
  study PDF.

## Rules S1–S19 (fixed before any code; tests in PR 1–5 pin them)
- **S1 · Identity comes from the server.** On every `/v1/widget/*` and `/v1/staff/*` route, the
  business and the customer or staff member come only from a session row read under that
  business's RLS. No header or body field names either. A session route that receives
  `X-Business-Id` answers 400 [proposal]. Request bodies are closed (`extra="forbid"`): a body
  naming `customer_ref`, `business_id` or any other extra field is a 422 that echoes nothing. A
  customer token on a staff route and a staff token sent as `Authorization: Bearer` on a widget
  route are both 401 (each lookup filters its `kind`).
- **S2 · The header seam stays a seam** [D5]. `get_business_id` and its 7 routes stay unchanged,
  accepted only in `HEADER_ENVIRONMENTS` (local, test, ci). Five red-team runner modules
  (`redteam/agent.py`, `answer.py`, `search.py`, `document.py`, `logs.py`), 30 test files
  (counted with `grep -rl`, 2026-11-24) and M3's goldens keep using them. The route-inventory test proves that
  production never reaches them.
- **S3 · Tokens.** A customer token is `<business_id>.<secrets.token_urlsafe(32)>`: 32 random
  bytes, 256 bits (arithmetic). Only its SHA-256 is stored, as with confirm tokens. The lookup
  parses the prefix with `parse_business_id` (`db.py:121`), opens that business's RLS transaction and reads
  `(business_id, token_sha256)` with `expires_at > now()` and `revoked_at IS NULL`. A prefix
  edited to another business is simply not found there: no `SECURITY DEFINER`, no `BYPASSRLS`.
  Every failure gets one fixed 401 body. Tokens, their hashes, cookies and `Authorization` never
  appear in a log line (the global log canary, extended).
- **S4 · `customer_ref` comes from the session.** It is `s-` plus the session uuid's 32 hex
  digits: 34 characters (arithmetic). That fits `CUSTOMER_REF_PATTERN` and 0006's CHECKs, so no
  earlier migration changes. M7's per-customer window becomes per session with no limiter
  change, and ADR 0016's IDOR closes: a session sees and confirms only its own actions and
  hand-offs. It is an identifier, not a credential.
- **S5 · Staff access** [D4]. Staff rows live in the database only (rule 5), created by an owner
  CLI with synthetic names. The cookie is `__Host-vp_staff`: `HttpOnly; Secure; SameSite=Strict;
  Path=/`, value hashed at rest like customer tokens. Roles are `reviewer` and `admin`, read from
  the row on every request, so disabling someone takes effect at once. **CSRF:** one dependency
  on every non-GET `/v1/staff/*` route, login included (login CSRF: a victim logged into a
  session the attacker chose): `Origin` equal to `public_origin` (an absent `Origin` is 403),
  `Sec-Fetch-Site: same-origin` when sent, and `Content-Type: application/json` wherever there is
  a body (FastAPI 0.143's `strict_content_type` default, True, stays). Origin is the control on
  bodiless requests (claim, logout). The route inventory asserts the dependency on every such
  route. A login link is used by one atomic `UPDATE ... SET used_at = now() WHERE ... AND used_at
  IS NULL AND expires_at > now() RETURNING` (two parallel uses: one wins, race-tested). Origins
  of staff and widget [D12]. No platform-operator cross-business access in M8.
- **S6 · Same guard, same services** [carried M7 G13]. `POST /v1/widget/messages` calls the
  `app.state.agent_guard` that `/v1/agent` uses, with an `AgentRequest` built server-side.
  Widget confirm uses the same confirm limits and `ConfirmService`. No new path to a model.
  `/v1/agent`, its confirm route and `AgentResponse` stay unchanged as the harness path. The
  widget routes take the same dependency functions (`get_agent_service`,
  `get_read_only_agent_service`, `get_agent_guard` in `agent/router.py:66-78`,
  `get_confirm_limits` in `actions/router.py:103`), so a test override wires both routes
  identically. They have **no unguarded branch**: `agent/router.py:97` (`if guard is None`) and
  `actions/router.py:124` (`if limits is not None`) are not copied. With guards off
  (`guard_routes = off`, local, test and ci) the message route answers a `guards_not_configured`
  hand-off (urgent with guidance for a health business) and widget confirm a typed 503
  (`limits_unavailable`), never the service; tested under `guard_routes = off`. ADR 0016 W2's
  "one response that carries the token" now names two: `/v1/agent` and `/v1/widget/messages`
  (ADR 0019).
- **S7 · Parity is the safety gate.** The red-team replay of "How we will know" runs in the `api`
  job, with M7's stand-in matchers and with the registered seams. While H1 is a stub, every
  agent-route case is `guard_error` on both routes, so the seam run alone would pass even if the
  widget skipped a guard step: the stand-in run is the meaningful one. The 2 confirm cases
  replay through widget confirm. Cases whose vehicle cannot pass through a session route
  unchanged (F's log vehicle, K's `k-003` on Redis, C's `ticket_text`, per noah) go into a closed
  exclusion list with ids and reasons, reviewed by zara and noah in PR 2. `scripts/parity_gate.py`
  (standalone, as `canary_gate.py`; M7's `redteam_gate.py` stays unchanged while M7's close-out
  still uses it) reads the JUnit report and fails unless every frozen agent-route and
  confirm-route id appears exactly once, run or excluded, with no skip, and an xfail only where
  `/v1/agent` has the same id with the same reason. The frozen cases and `freeze.json` stay
  unchanged; only harness code is added.
- **S8 · Honest hand-offs** [carried §3.5, §4.3]. Guidance comes first, verbatim, from the source
  the guards already use (`data/safety/businesses/<id>.json`, kept equal to `tools.json` by
  M7's test), never re-typed into widget configuration, and shown with `lang="en"` (the files
  are English only; localised guidance waits for a reviewed safety file, business-context Q8).
  "A person will reply" is shown only when the hand-off was stored (`queued: true`) and is not
  urgent; otherwise the business's configured contact fallback. **Urgent hand-offs never invite
  waiting** [D11]: for `priority = urgent` and every `safety` reason the widget shows no generic
  follow-up, no ETA, no reply prompt, does not poll, and drops any open confirmation card (the
  action expires unexecuted, P3), because both safety files end with "Do not wait for a reply in
  the chat." (`sunrise-clinic.json:10`, `paws-and-co.json:9`). No emergency number is hard-coded
  anywhere (rule 9). A health-type business with `enabled: true` and empty guard guidance is
  refused at startup; a non-health business without guidance shows none.
- **S9 · Answers only with sources; untrusted text stays text** [carried ADR 0011, 0017 E7]. The
  widget shows an `answer` only when the outcome is answered and `sources` is non-empty; an
  unknown `kind` becomes the safe hand-off state. Model text, chunks, guidance, tool summaries,
  extraction values and spans render as React text: no `dangerouslySetInnerHTML`, no markdown,
  no auto-linking. Enforced, not only agreed: ESLint `react/no-danger: error` and `no-console`
  on the product pages (`eslint-plugin-react` is already installed through `eslint-config-next`;
  `no-console` is core: no new package), and a fixture per state whose text holds
  `<img src=x onerror=alert(1)>` and `javascript:alert(1)` with an assertion that no element or
  attribute is created. **Headers** (new pages and routes only, S13): the pages get
  `Content-Security-Policy: default-src 'none'; script-src 'self' 'unsafe-inline'; style-src
  'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; font-src 'self'; base-uri
  'none'; form-action 'none'; frame-ancestors <the business's origins | 'none'>` (the widget's
  `connect-src` narrowed under D12 (b)), `X-Content-Type-Options: nosniff` and
  `Referrer-Policy: no-referrer`; every `/v1/widget/*` and `/v1/staff/*` response gets
  `Cache-Control: no-store` (masked messages, replies and tokens never sit in a disk cache).
- **S10 · Never the unguarded paths.** The widget never calls `/v1/answer` or `/v1/kb/search`
  (their guards are off until M7 PR 10), nor `/v1/agent`. A test pins the API client's route list.
- **S11 · Stored text is masked, scoped and expires** [D6]. The hand-off table is the first
  place customer text rests. A red-flag hit returns before the guard's own masking step (G1),
  so the route masks independently, through one shared helper on the same input the guard
  uses: `mask_pii(prepare(raw).clean, locales())` (`guards/pii.py:97`, `guards/screens.py:234`,
  `guards/locales.py:493`). NULL when masking fails or the text is over the cap (`OverCap`,
  `guards/caps.py:56`), never raw and never truncated. Category E and F replay through it
  ("How we will know"); the widget route and a forced store failure join category F's log path
  (`redteam/logs.py`'s vehicles). Retention is per business; RLS hides rows at expiry, and the
  owner's purge (by hand, as M6) NULLs the text columns (`message_masked`, `guidance_shown`,
  `reply`) and keeps the metadata (reason, priority, origin, label, times), so labels outlive
  retention for M11. Synthetic data only until Q4c.
- **S12 · New tables follow 0006/0007.** Non-null `business_id` leading the primary key, `ENABLE`
  and `FORCE` RLS with the one `business_isolation` policy, unique keys and FKs that pair the
  business, column-scoped grants, no DELETE or TRUNCATE for the app role, CHECKs that mirror the
  code (tests compare), and a downgrade tested in a rolled-back transaction.
- **S13 · M3's window and M4–M7's pending runs** [carried M3 D3, M7 G13]. No change to
  `gateway/models.toml`, `PROMPT_VERSION`, `answer/prompt.py`, the gateway's code or output
  path, `/v1/answer`'s or `/v1/kb/search`'s routes and responses, `agent/loop.py`, `intents/`,
  `extraction/routing.py`, `guards/match.py`, the term files or the frozen red-team cases. No
  streaming (it changes the output path; ADR 0006 moved hands-on streaming to M8, the window
  moves it again). No history to the model: each turn sends one message, so
  `AGENT_PROMPT_VERSION` and H1 `run_turn`'s interface stay. Security headers go on the new
  routes and UI pages only, never on existing routes. Each PR's acceptance shows the diff-stat.
- **S14 · Emergencies need no Redis.** Scope: messages on an existing session. Such a message
  needs the API process and Postgres only: the session lookup is Postgres, the red-flag screen is
  first (M7 G1). A session is created lazily, on the first send or "talk to a person", never on
  page load (a page view must not spend the per-business creation window). Session creation is
  rate-limited in Redis and fails closed; the widget then shows the business's guidance (cached
  from `GET /v1/widget/{business_id}/config`, which reads files only) and its contact fallback,
  never "a person will reply". A failed session lookup (Postgres down) is a typed 503, never a
  500. The hand-off store write has its own short timeout (a setting), so a hung insert cannot
  push the guarded response past the client timeout. Each case is a test (Redis down, including
  a first message with a red flag; Postgres down; a hung store).
- **S15 · Accessibility.** WCAG 2.2 AA on both UIs, including 2.4.11, 2.5.7, 2.5.8, 3.2.6 and
  3.3.8 (sofia's targets, §4, carried into PR 4 and PR 5). Reduced motion is always respected;
  guidance never animates in either mode.
- **S16 · Guidance in every non-success state.** The widget fetches the config route when the
  greeting renders and keeps the guidance in memory. For a business with guidance, it is shown
  first (`role="alert"`, `lang="en"`) in every state that is not a success: network error,
  client timeout, 401 or session expired, 422 (over length), 429 (the body's guidance, else the
  cached copy), 5xx and 503, an unknown kind, and failed session creation. The typed message is
  kept, so a retry loses nothing; the widget never re-sends a message by itself (D15). Over
  length is checked in the client with the server's unit, code points (`Array.from(text).length`,
  `MAX_QUESTION_CHARS` 2000, `retrieval/types.py:40`), not graphemes: a Telugu or Hindi text can
  look shorter than it is. This closes M7's over-length J gap for the widget.
- **S17 · One open hand-off per session.** A red-flag hit skips the limiter (M7 G1) and
  `POST /v1/widget/handoffs` has none, so a partial unique index on `(business_id, session_id)
  WHERE status <> 'resolved'` bounds the queue by sessions, and sessions by the creation window.
  A later hand-off on that session increments `count`; when its priority is higher, it replaces
  the row's reason, origin, `trace_id`, message and guidance (priority only ever rises; a
  claimed row stays claimed and shows the raise). The customer's response, guidance included, is
  unchanged. "Talk to a person" returns the open row's `{id, queued: true}` (idempotent per
  session). Polling and feedback get per-session Redis windows that fail closed (neither is the
  emergency path).
- **S18 · What the browser keeps.** `sessionStorage` holds the session token and nothing else.
  The transcript lives in component memory only; a reload starts a fresh view. A confirm token
  lives only in memory, bound to its `action_id`: never in Web Storage, IndexedDB, the URL, the
  DOM, the console or a `postMessage`. The widget registers no `message` listener in M8 (a host
  page, untrusted, can never drive Confirm). "End chat" revokes the session and clears state. No
  message text goes into `console`, `document.title` or the URL; the dashboard's announcements
  and title carry priority and count only. The widget page reads only `lang` from its query, and
  the embed snippet says nothing else may be passed (host data in the iframe `src` would land in
  the API's access log).
- **S19 · Counts, not rates** [carried rule 3]. In M8 the dashboard shows counts only, never
  rates or percentages, and every screenshot taken from fixtures is captioned "fixture data".

## Out of scope, and playbook deviations
- **M3's window** (S13). No streaming, no conversation history to the model, no change to the
  answer path. Answers reach a customer only as `/v1/agent`'s `answer` kind, under
  `agent_router = on`.
- **Not in M8** [proposal]:
  - production hosting, a domain, TLS and edge or IP limits (M10);
  - production staff onboarding and password or SSO login (M10, D4);
  - KB upload, approve and retire from the dashboard, and Q12's retire-only step (the admin
    screen comes later, Q3);
  - staff confirming a customer's action (only the customer holds the token, ADR 0016 W2), MCP
    confirm, cancel as a status (dropping the token is the cancel, P3), refunds, "draft for
    staff";
  - collecting customer contact details (personal data, Q4c);
  - WhatsApp (Q5);
  - localised confirmation summaries (`summary_language` stays `"en"`, shown with `lang="en"`);
  - the escalation-precision metric (D10) and the wrong-answer label (D14);
  - "repeat within 24 h", and so the auto-resolution rate: not measurable from M8's data, because
    an anonymous session lives in one tab's `sessionStorage` and two visits by one customer are
    never linked (D13);
  - a confirm path for MCP proposals (ADR 0016's revisit trigger, answered in ADR 0019: MCP
    stays flag-scoped and local, test and ci only, with no session and nothing to confirm);
  - linking a session to an existing customer: customer-scoped tools see only what this session
    created (ADR 0019);
  - a Next.js server, a new Docker image or service (D2).
- **Deviations.**
  - The roadmap names M8 "Staff dashboard"; M8 also builds the widget and sessions, because
    the dashboard has nothing to review without them.
  - "Corrections become evals" means exported candidates, never automatic dataset entries. The
    README (line 30, "every correction becomes an eval case"; line 33's tracing is M9's) and the
    site's M8 summary (`roadmap.ts:79`) change accordingly (PR 0, with the main session).
  - §6 (signed off 2026-10-13) places the wrong-answer rate in "Staff review (M8)". Under D14 (a),
    decided by shesha 2026-11-24, it moves to M11: a recorded deviation from a signed-off row.
  - The real stack shows only hand-offs in M8 (Facts, "Fail closed today"). Every other widget
    state is proven on fixtures, and the note says so.

## Facts this plan relies on (checked 2026-11-24 on `m8/plan` at bbdf5c6)
- **Business identity.** `business.py:24` (`HEADER_ENVIRONMENTS = {local, test, ci}`), `:32`
  (`get_business_id`), `:38` (other environments refused), `:52` (503 when Postgres fails).
  The 7 header routes: `answer/router.py:87`, `kb/router.py:168,192` (documents, reindex),
  `retrieval/router.py:149` (`/v1/kb/search`), `agent/router.py:87`, `actions/router.py:115`
  (confirm), `extraction/router.py:270`. ADR 0009:33-36 ("until M8 auth replaces it").
- **Customer identity.** `tools/config.py:55-56` (`CUSTOMER_REF_PATTERN`, length 3–40),
  `:65-72` (`CustomerRef`); `agent/responses.py:112-118` (`AgentRequest` takes it from the
  body); migration 0006:136-138 (the CHECK on `pending_actions` and three sandbox tables).
  `guards/limits.py:4-11` (key grammar; per-customer window "advisory"), `:166`
  (`customer_hash`), `:176-179` (`rate_key`). ADR 0016:167-172 (the IDOR closes when M8
  authenticates).
- **Hand-offs.** `agent/responses.py:51-92` (`HandoffReason`, closed), `:94-102`
  (`GUARD_HANDOFF_REASONS`), `:141` (`summary_language: "en"`), `:156-163` (`HandoffResponse`
  with guidance). `tooleval/metrics.py:52,63` (`DECISION_HANDOFF_REASONS`,
  `INFRASTRUCTURE_HANDOFF_REASONS`; the second includes the guard set). Migrations 0001–0007
  hold no hand-off, conversation, staff, session or feedback table.
- **Fail closed today.** `guards/agent.py:552-562`: a stub or failing guard gives `guard_error`,
  urgent for a health business when the red-flag screen did not clear. H1 `match_terms` is a
  stub, so every guarded `/v1/agent` message hands off. Both sample businesses are health types
  (`data/sample/sunrise-clinic/manifest.json`: `human_healthcare`, en and hi;
  `paws-and-co`: `veterinary`, en and te). `agent_router` is off (`config.py:271`), so after H1
  W11 still hands off (`safety_floor_off`). A non-health business would reach M5's H1 stub
  (`agent/loop.py`) and hand off as `agent_error`. `guard_routes = "on"`, `guard_answer = "off"`
  (`config.py:294,298`).
- **Extractions.** Migration 0007:37-38: SELECT and column INSERT only, no UPDATE ("M8's review
  decisions get their own migration"); rows hidden at expiry; file bytes never stored (ADR 0017).
- **Migrations.** Head is 0007. 0008 is named for M7 PR 11 (`m7-pr-checks.md:1308-1311`), not
  merged; that PR waits on M6's live runs.
- **Settings and app.** `config.py:18` (`Environment`), `:224` (`action_lifetime_seconds` 900),
  `:264` (`agent_turn_timeout_seconds` 35), `:279` (`agent_router_timeout_seconds` 15), `:318,321`
  (limits 120 per business and 10 per customer per minute). `main.py:324` (Redis), `:527-533`
  (seven routers), `:537` (one middleware, `RequestScope`). No CORS, cookie, origin or session
  setting exists.
- **Goldens.** `tests/answer/goldens.py:9-13`: for each of 38 recorded exchanges on `/v1/answer`
  and `/v1/kb/search`, the status, every response header in order, the exact body and the log
  lines (`tests/answer/goldens/guard_answer_off.json`).
- **Length.** `retrieval/types.py:40` (`MAX_QUESTION_CHARS` 2000); `m7-pr-checks.md:92` (an
  over-length emergency gets a 422 without guidance; "M8's widget enforces the length").
- **Masking.** `guards/pii.py:97` (`mask_pii`, implemented, H2).
- **From the round-2 reviews** (each re-checked by maya, 2026-11-24). Both safety files end
  their guidance with "Do not wait for a reply in the chat." (`sunrise-clinic.json:10`,
  `paws-and-co.json:9`). A red-flag hit reuses `safety` (`agent/responses.py:80`), and
  `_failed` makes `guard_error` urgent for a health business whose screen did not clear
  (`guards/agent.py:552-563`). Unguarded branches: `agent/router.py:97`, `actions/router.py:124`;
  dependency functions `agent/router.py:66-78`, `actions/router.py:103`.
  `scripts/redteam_gate.py:7-9` reads only `tests/redteam/test_cases.py`'s ids. `OverCap`
  (`guards/caps.py:56`), `prepare` (`guards/screens.py:234`), `locales` (`guards/locales.py:493`),
  `leak_forms` (`redteam/pii.py:325`). `config.py:26-43`: `/v1/agent` has no outer timeout; a
  `rag` route under `agent_router = on` can take about 151 s with the defaults, "whether to bound
  it too is open for shesha" (D15). `evalrun/intervals.py:83` (`wilson_interval`). Both sample
  manifests say `"synthetic": true` (line 5). MCP imports `HEADER_ENVIRONMENTS`
  (`packages/mcp_server/src/vectapilot_mcp/startup.py:30,71`) and its tests refuse other
  environments (`tests/test_startup.py:37`); ADR 0016:226 ("M8 authenticates customers:
  `customer_ref` from the session; a confirm path for MCP proposals"). `eslint-plugin-react`'s
  `no-danger` rule is on disk under `apps/web/node_modules` (through `eslint-config-next`).
  business-context §6: "resolved" at lines 248-250 (24 h repeat), wrong-answer rate "Staff review
  (M8)" at 259, thumbs-up and auto-resolution at M11.
- **Packages.** `uv.lock`: `fastapi` 0.143.0, `starlette` 1.7.0; neither `itsdangerous` nor
  `python-multipart` is locked. diego checked `hashlib.scrypt`, `StaticFiles`, `FileResponse`
  and `set_cookie`'s `samesite` and `partitioned` in the venv (his targets, 2026-11-24).
- **Web.** `apps/web/next.config.ts`: `output: "export"`, `basePath` from
  `NEXT_PUBLIC_BASE_PATH`. `apps/web/package.json`: `next` 16.4.5, `react` 19.3.0, `motion`,
  `@radix-ui/react-dialog`; no test runner. `ci.yml:178-197`: `web (lint, build)`, a required
  check (renaming it breaks branch protection). `ci.yml:83,88`: canary and red-team gates.
- **Compose.** `infra/compose.yaml:89-98`: the `api` service's read-only bind mounts (sandbox,
  sample, extraction, safety). A new mount takes effect at the next `make up`.
- **Disk.** `df -h /`: 11 GiB available; `du -sh apps/web/node_modules`: 470 MB (both
  2026-11-24).
- **Earlier commitments to M8.** ADR 0002 ("the customer widget and dashboard will need a server
  later (separate decision)"); ADR 0006:60 (streaming moved to M8, concept only); ADR 0016:182
  (server-side history: "M8's widget"); `m7-pr-checks.md:309,1218` (whether a customer surface
  may call search is M8's question: no, S10), `:1234` (a transcript fed to the loop needs H3's
  union of boundary lines: not in M8, S13). business-context §3.2 (web chat: allowed embedding
  sites, branding), §3.5 (priority drives the queue; the customer is told honestly), §4.3 (the
  customer knows it is an AI and is one tap from a human), §5 (staff see only their business),
  §8 (M8: dashboard scoped to one business; admin screen later), Q9 and Q12 (open).

## Order of work
```
Decided:  D1–D15 (shesha, 2026-11-24, all as recommended)
Now ($0): PR 0 plan + ADR 0019                                       S
Then:     PR 1 sessions + staff auth (diego)                         M  → after D1, D3, D4, D5, D12
          PR 2 widget API + hand-off store + UI serving (diego)      L  → after PR 1, D2, D6, D7, D11, D15
          PR 3 staff API + extraction review (diego)                 M  → after PR 2, D14
          PR 4 widget UI (sofia)   starts after PR 1 on fixtures,    L  merges after PR 2 (D8, D9, D11)
          PR 5 staff UI (sofia)    starts after PR 2 on fixtures,    M  merges after PR 3
Measure:  PR 6 close-out (local smoke, manual accessibility pass,    S
          bundle sizes, §6 table, note)
```
That is 5 team PRs before the close-out, each reviewed by shesha. Sizes are estimates. PR 2 and
PR 4 are L after the round-2 tests; each has one named split point, used only if its review
asks for it (PR 2a: messages, hand-off store, parity and PII; PR 2b: confirm, turns, feedback,
UI serving · PR 4a: state logic, unit tests, i18n; PR 4b: the `web e2e` job). PRs 1 → 2 → 3 are
serial (one migration chain). The UI PRs run in parallel against fixtures that a pytest guard
keeps equal to the API's models. Under D10 (a) M8 has no `[HAND-WRITE]`. Nothing waits on M7's
H1: route tests use M7's stand-in matchers, as M7's own tests do. D13 changes no PR.

## PR 0 — `m8/plan` (maya)
- This file, ADR 0019 (proposed; accepted once D1–D5 are decided), and `currentMilestone = "M8"`
  in `apps/web/src/data/roadmap.ts`, with M7's pending note "H1, live run and close-out pending".
- With the main session: CLAUDE.md (M8 status, ADR 0019), the README's M6–M8 rows (M6 and M7
  still read as planning) and its lines 30 and 33 ("Staff review hand-offs and extraction fields;
  corrections are exported as eval candidates a person reviews"; tracing is M9's), the site's M8
  summary at `roadmap.ts:79` ("corrections exported as eval candidates"; if cut 5 happens, the
  pending note says so), shesha's decisions in a "Decided" section here.
- On acceptance of ADR 0019, one line each in ADR 0002, 0006, 0009, 0016 (W2 names both routes;
  the revisit trigger at :226 answered) and 0018 pointing to it, and in `m7-pr-checks.md` PR 11:
  "migration 0008" becomes "the next free number" (P4).

## PR 1 — `m8/sessions` (diego; zara reviews tokens and auth, leo the `customer_ref` change and MCP) · M · after D1, D3, D4, D5, D12
- **Migration M8-a `sessions_and_staff`** (S12):
  - `staff_users(business_id, id, display_name 1–80, role ∈ {reviewer, admin}, created_at,
    disabled_at)`; the app role may only SELECT, the owner CLI inserts.
  - `staff_login_links(business_id, id, staff_id, token_sha256, created_at, expires_at ≤
    created_at + 1 h, used_at)`, `UNIQUE (business_id, token_sha256)`; the app role has SELECT
    and UPDATE(`used_at`), used only by S5's atomic statement [D4 (a)].
  - `sessions(business_id, id, kind ∈ {customer, staff}, staff_id set exactly when staff,
    token_sha256, channel ∈ {web}, ui_language (a short language tag; the widget config's list
    is checked in code), created_at, expires_at ≤ created_at + 24 h by CHECK, revoked_at)`,
    `UNIQUE (business_id, token_sha256)`; INSERT on named columns with `expires_at` from the
    database clock, UPDATE(`revoked_at`). Whether expired rows are hidden by a restrictive
    policy or by the lookup's clause is verified against Postgres here (diego's note on
    `UPDATE ... RETURNING` with restrictive policies).
- **`auth/`** (standard library only): token make, parse and hash; `get_customer_session`
  (replaces `get_business_id` on widget routes; returns `business_id`, `session_id`,
  `customer_ref`, `ui_language`; filters `kind = 'customer'`); `require_staff(role)` (filters
  `kind = 'staff'`); S5's CSRF dependency; cookie helpers. A failed lookup is a typed 503 (S14).
- **Widget configuration** (rule 9): `data/widget/businesses/<business_id>.json`
  (`VECTAPILOT_WIDGET_CONFIG_DIR`, optional; a business without a file has no widget), validated
  at startup: `enabled`, `allowed_origins`, `languages`, `display_name`, `greeting`,
  `handoff_follow_up` and, under D11 (b), `urgent_follow_up`, per configured language (Q9's text
  lives here), `contact_fallback` lines, `quick_replies` (optional, cut 2),
  `handoff_retention_days` 1–90, and an optional `accent_color` refused below 4.5:1 against the
  text colour [proposal]. Each origin is parsed strictly as `scheme://host[:port]` (no path,
  wildcard, whitespace, `;` or `,`; https except `localhost` and `127.0.0.1` in local), because
  it is written into a CSP header. A health-type business with `enabled: true` and empty guard
  guidance is refused (S8). Guidance is not copied here (S8). Synthetic files for the two sample
  businesses. Compose gets one more read-only mount (sofia reviews).
- **Settings** (fail fast, typed errors without values; each joins `TEST_SETTINGS_FIELDS`):
  `public_origin` (required in production), `staff_origin` only under D12 (a),
  `widget_config_dir`, `ui_dir`, the three lifetimes (P1), the limit for session creation (staff
  login's was dropped in step B3), `cookie_insecure_dev` (local and test only, refused elsewhere
  at startup) and `cors_dev_origins` (local only; cut 6).
- **Routes:** `GET /v1/widget/{business_id}/config` (files only: no database, no Redis; returns
  the guidance for S16), `POST /v1/widget/sessions {ui_language}` (Origin = `public_origin`; the
  business in the registry and its widget enabled; the per-business creation window; returns the
  token, lifetimes and the client timeouts for messages and confirm, P7), `DELETE
  /v1/widget/session`, `POST /v1/staff/login`, `POST /v1/staff/logout`, `GET /v1/staff/me`. A new
  session on every login (no fixation). Bodies `extra="forbid"`; `Cache-Control: no-store` (S9).
- **MCP** [proposal, leo]: `--customer-ref` refuses the session form `s-<32 hex>`, so a local
  MCP run cannot read a widget session's bookings. `packages/mcp_server` only; `/v1/agent` is
  untouched.
- **Owner CLI:** `python -m vectapilot_api.staff add|issue-link|disable` (owner role; local, test
  and ci only until M10). `issue-link` prints `<public_origin>/staff/login#b=<id>&t=<token>`:
  the fragment never reaches a server log or `Referer`.
- **Limiter:** `RouteSlug` gains `session` (and `staff_login`, dropped in step B3); the closed key
  grammar and the K-004 key scan cover it unchanged.
- **Tests:**
  - Canary, under `tests/canary/` (the gate's no-skip rule): the new tables named in the catalog
    assertion; A's token with B's prefix → 401 in both directions; one uuid planted in A and B is
    invisible across them; expired and revoked tokens → 401; the session lookup and the link use
    in `OWNER_CONNECTION_PATHS`; header and body smuggling (S1).
  - Route inventory under production settings (S1, S2); the role map covers every
    `/v1/staff/*` route; S5's dependency on every non-GET staff route, login included; a
    customer token on a staff route → 401 and a staff token as Bearer on a widget route → 401.
  - CSRF on login and logout: wrong or absent `Origin` → 403, cross-site `Sec-Fetch-Site` → 403,
    a form or `text/plain` body → 4xx; two parallel uses of one link → one session (race).
  - One refusal test per widget-config rule (each bad origin form; health business without
    guidance); `extra="forbid"` → 422 with no echo.
  - One 401 body for every failure; header plus session → 400; log canary over tokens, hashes,
    cookies and `Authorization`; `Cache-Control: no-store` on every new response.
  - MCP: `--customer-ref s-<32 hex>` refused.
  - Rule 7: whether browsers keep a `__Host-`/`Secure` cookie on `http://localhost` and
    `http://127.0.0.1` is checked by hand in Safari and Chromium and written into the PR; the
    fallback is `cookie_insecure_dev`.
- **Acceptance also shows:** `make check`; `make mcp-check`; `make test-integration` locally, then
  the database back at 0005 (S12's downgrade); the S13 diff-stat empty; no dependency change.
- **Step B1 (built 2026-11-24, diego): migration 0008, `auth/`, Settings, canary.** Routes, the
  widget files and their compose mount, the owner CLI, the MCP refusal, the limiter's slugs, the
  route inventory and the per-route log canary followed in step B2 (below).
  - **Expiry, decided here:** a restrictive `SELECT` policy `hidden_at_expiry` (app role,
    `expires_at > now()`) on `sessions` and `staff_login_links`, as 0007, **and** the same clause
    in every statement (the owner's connection has no policy). Revocation and use stay in the
    statements' clauses only: Postgres checks `SELECT` policies against an UPDATE's new row when
    the UPDATE reads the table (WHERE or RETURNING), so a policy `revoked_at IS NULL` or
    `used_at IS NULL` would refuse S5's `UPDATE ... RETURNING` with 42501. Verified against
    Postgres by `tests/test_migrations.py` (the refusal is a red proof there), pending the first
    integration run.
  - [proposal] `ui_language` is set exactly for customer sessions (a CHECK); a staff session has
    none. `channel` is `web` for both.
  - [proposal] A role that may not use a staff route is 403 `role_refused` (authenticated, not
    allowed), apart from the one 401. CSRF answers 403 `cross_site_refused`, a body that is not
    `application/json` 415 `json_only`; `X-Business-Id` on a session route 400
    `business_header_refused`. The widget's 401 adds `WWW-Authenticate: Bearer`; its body is the
    staff 401's, byte for byte. Every auth answer carries `Cache-Control: no-store`.
  - [proposal] `admin` may use every `reviewer` route (P2's "plus").
  - [proposal] A login link of a disabled member is still consumed; the staff lookup refuses that
    member on every request, so the session it creates opens nothing. (Tightened in step B3: the
    login opens no session at all for such a member, and answers the one 401.)
  - [proposal] `public_origin` is required in production by `create_app`
    (`require_public_origin`), not by Settings: the MCP server and the CLIs never serve a session.
    Origins are strict: lowercase, no default port, no IP but 127.0.0.1, no IPv6; http only for
    `localhost` and `127.0.0.1` outside production.
  - [proposal] New setting `session_lookup_timeout_seconds` (2 s, a safety bound): past it, S14's
    typed 503. Limit placeholders: 60 new sessions and 10 staff logins per business per minute
    (the staff-login window dropped in step B3).
  - [proposal] Session and login-link tokens share the form `<business_id>.<secret>`, so the
    CLI's `#b=<id>&t=<token>` names the business twice; the CLI step decides whether `b=` stays.
  - [proposal] `auth/` joins the coverage gate's targets (100 % combined and branch today).
- **Step B2 (built 2026-11-24, diego): routes, widget files, owner CLI, MCP refusal, limiter
  slugs, route inventory, log canaries.** `widget/` (`config`, `models`, `router`), `staff/`
  (`router`, `owner`, `cli`), `auth/http.py` (`SessionRoute`: `no-store` and a closed 422 on the
  new routes only), `data/widget/` (two synthetic files and a README), one read-only mount and
  `VECTAPILOT_WIDGET_CONFIG_DIR` in `infra/compose.yaml`, `RouteSlug.SESSION` (and `STAFF_LOGIN`,
  dropped in step B3). The integration tests (`tests/canary/test_session_routes.py`, the login-link race through the
  real route included, and `tests/staff/test_cli_db.py`) are collected, not yet run.
  - [proposal] `POST /v1/widget/sessions` takes `{business_id, ui_language}`: the one request
    before a session exists, so it names the business (as the widget page's URL does; anyone may
    open a session for an enabled business, ADR 0019 Consequences). Every later widget request
    takes it from the session, and S1's "a body naming `business_id` is a 422" holds on every
    other session route. The alternative is `POST /v1/widget/{business_id}/sessions`, as the
    config route.
  - [proposal] Each route may have its own per-business window (`LimitPolicy.routes`): session
    creation uses its own, every M7 route keeps `business`; keys and the script are unchanged.
    (B2 gave staff login one too, keyed by the token's business prefix; step B3 dropped it.)
  - [proposal] Order on every session route: a body FastAPI cannot decode is refused first (400
    or 422, before any check runs, writing nothing; step B3), then `X-Business-Id` (400), then
    the Origin check, then the body model, then the lookup; logout included. A session route
    turns only a database or timeout error into S14's 503; anything else is a 500 (the log
    canary's forced path), as on M7's routes. Session creation's 429 carries a health business's
    guidance (G3); its 503 does not (the widget shows its cached copy, S16). A 404 or 405 from the
    router itself under the new prefixes has no `no-store` (it holds no data; a middleware would
    touch every route, S13).
  - [proposal] Widget files: an enabled widget names at least one origin. The samples name
    `https://www.<business_id>.example` only (RFC 2606), so they load in every environment; an
    `http://localhost` origin loads only in `local`, as written above. The samples' texts:
    greeting, contact fallback (the corpus's fictional numbers) and two quick replies per
    language; hi and te drafted by Claude and labelled unreviewed (D9); both follow-ups left out
    for shesha (Q9, D11 (b)). Accent colours `#0f5c8c` and `#7a3e00` against white text, about
    7.2:1 and 8.3:1 (arithmetic by the loader's own function); white as the text colour is for
    sofia to confirm in PR 4.
  - [proposal] The login link keeps `#b=<business_id>&t=<token>`: the page can name the business
    before any request; the API reads only the token.
  - [proposal] `VECTAPILOT_PUBLIC_ORIGIN` is not in compose yet (this step's compose change is the
    mount): without it every session POST answers 403. PR 2, which serves the pages, adds
    `http://localhost:8000`.
  - **For shesha (rule 7, by hand, not done by Claude):** whether Safari and Chromium keep the
    `__Host-vp_staff` cookie (`Secure`) on `http://localhost:8000` and on `http://127.0.0.1:8000`.
    With the API running with `VECTAPILOT_PUBLIC_ORIGIN` set to that origin and a synthetic member
    (`uv run python -m vectapilot_api.staff add`, then `issue-link`), open `<origin>/docs` in each
    browser, run `fetch('/v1/staff/login', {method: 'POST', headers: {'content-type':
    'application/json'}, body: JSON.stringify({token: '<the t= value>'})})` in its console, then
    `fetch('/v1/staff/me')`: a 200 means the cookie was kept and sent, a 401 that it was dropped
    (also visible under the browser's stored cookies). Repeat with a fresh link per try; if a
    browser drops it, repeat with `VECTAPILOT_COOKIE_INSECURE_DEV=true` (cookie `vp_staff`).
    Write the browser versions and the four results into the PR.
- **Step B3 (2026-11-24, diego): zara's and leo's review findings and the mutation probes.**
  Applied as found (each tightens a proposal; none changes a decided item, D1–D15 or P1–P7):
  - **One-way revocation and use** (migration 0008, not yet merged, so edited in place): two
    RESTRICTIVE `UPDATE` policies for the app role, `revoke_once` on `sessions` and `use_once` on
    `staff_login_links` (`USING (col IS NULL) WITH CHECK (col IS NOT NULL)`), as 0006's
    `app_writes_are_actions`. An un-revoke or un-use updates no row; clearing the column on a live
    row is 42501. `UPDATE` policies, not `SELECT` ones, so B1's `UPDATE ... RETURNING` trap does
    not apply.
  - **A disabled member's link opens nothing**: S5's link use reads the member in the same
    statement (a scalar subquery that pairs the business, so a lost pairing raises SQLSTATE 21000
    on the owner's connection instead of choosing); for a disabled member the login commits the
    use, stores no session and answers the one 401. The owner's `disable` also revokes the
    member's live sessions in the same transaction, so a hand re-enable revives none.
  - **No staff-login window** (zara's recommendation; her finding asked shesha, but it changes no
    decided item, so it is applied and shown here for review): a window keyed by the token's
    business prefix, which the caller writes, runs before any lookup and only lets anyone lock a
    business's staff out (and makes login depend on Redis). Removed:
    `staff_login_limit_business_per_minute`, `RouteSlug.STAFF_LOGIN`, the login's limiter call.
    The link's 256 bits and single use are the control; per-IP limits are M10's (Risks). To
    reverse: restore the setting, the slug and the call, and record the lockout in the note.
  - **An undecodable body is typed**: `SessionRoute` also answers FastAPI's own `HTTPException`
    (a body that is not UTF-8, 400) with a typed `{detail, error: "invalid_request"}` body of
    fixed text and `no-store`. The documented order now starts with it.
  - **Origins**: an IPv4 host is refused by WHATWG's "ends in a number" (decimal or `0x` hex last
    label), so `https://0x7f000001` and `https://1.2.3.0x4` are refused like `https://10.0.0.1`.
  - **Canary**: the revocation joins `AUTH_PATHS` with a red proof on the owner's connection, the
    link use's member pairing gets its own red proof (21000), and a login with an expired link
    joins the route tests (the one 401, the link unused, no session row).
  - **Leo**: the route inventory walks every session route's request-body schema (no
    `customer_ref`, `session_id` or `staff_id`; `business_id` only on session creation), so PR 2's
    message and confirm routes are checked the day they exist; the MCP process may not load the
    session code (`NEVER_LOADED`); the MCP's refusal text says "asserted, never authenticated"
    (no change is coming after M8); G9's docstring says what changes for a session reference.
  - **Mutation probes**: one test per surviving non-equivalent mutant (11), each shown to fail on
    its mutant and pass on the restored code.

## PR 2 — `m8/widget-api` (diego; leo reviews confirm, zara parity and honesty, noah turns and feedback) · L · after PR 1, D2, D6, D7, D11, D15
- **Migration M8-b `handoffs_and_turns`:**
  - `handoffs(business_id, id, session_id, trace_id, origin ∈ {turn, button}, reason ∈
    HandoffReason values, priority ∈ {urgent, high, normal}, count ≥ 1, intent, channel,
    ui_language, guidance_shown (the lines sent, not re-read later), message_masked (≤ 2,000
    characters, NULL when masking failed or over the cap), status ∈ {open, claimed, resolved},
    claimed_by/at, resolved_by/at, staff_label ∈ {needed, not_needed, unsure}, reply/replied_at
    (D7 (a)), retention_days 1–90, created_at, expires_at generated as in 0007)`. CHECKs:
    `origin = 'button'` implies `reason = 'customer_asked'`; `staff_label` is NULL when the reason
    is a system reason (`INFRASTRUCTURE_HANDOFF_REASONS`) or `origin = 'button'`. Partial unique
    index `(business_id, session_id) WHERE status <> 'resolved'` (S17). Index `(business_id,
    status, priority, created_at)`; a restrictive SELECT policy hides rows at expiry; UPDATE only
    on the status, label, reply, raise (S17) columns, and the purge's text columns for the owner.
    Tests pin the reason CHECK equal to `HandoffReason` and the system set equal to tooleval's.
  - Under D6 (a): `widget_turns(business_id, id, session_id, trace_id, kind, reason, priority,
    channel, ui_language, route_latency_ms, created_at)`, `UNIQUE (business_id, trace_id)`, no
    text, written at the route outside the measured service; a failed write is never a 5xx.
    `turn_feedback(business_id, id, session_id, trace_id, rating ∈ {up, down})` with an FK to the
    turn (paired with the business) and `UNIQUE (business_id, trace_id)` (unless cut 4).
- **Routes** (session required, S1; bodies `extra="forbid"`; `Cache-Control: no-store`):
  - `POST /v1/widget/messages {message}` → `{turn: AgentResponse, handoff: {id, queued} | null}`
    through the same guard and dependency functions (S6) and the same 429 mapping, with no
    unguarded branch. Under D15 (a), one outer bound at the route; past it, a `turn_timeout`
    hand-off (urgent with guidance for a health business). On a hand-off the route stores or
    raises the session's row (S17) after the guarded response is built, outside the measured
    service, masking through S11's helper and with S14's store timeout. A failed store gives
    `queued: false`, never a 5xx. The turn row is written the same way.
  - `POST /v1/widget/handoffs`: "talk to a person", `customer_asked`, `origin = button`, normal,
    no model call; idempotent per session (S17).
  - `GET /v1/widget/handoffs/{id}`: status (and the reply under D7 (a)), this session's only; a
    per-session Redis window (fails closed).
  - `POST /v1/widget/actions/{id}/confirm {confirm_token}`: `ConfirmRequest` built server-side
    with the session's `customer_ref`; the same `get_confirm_limits`; outcomes and errors as the
    confirm route; no `limits is None` branch (S6).
  - `POST /v1/widget/feedback {trace_id, rating}` (cut 4): only for this session's own turns
    (D6 (a)); one rating per turn; a per-session Redis window (fails closed).
- **UI serving** (D2 (a), D3 (a)): `GET /widget/{business_id}` returns the widget build's page
  with S9's headers and `frame-ancestors` from that business's `allowed_origins` (and, under
  D12 (b), `connect-src` narrowed to `<public_origin>/v1/widget/`); `/staff/` returns the
  dashboard build with `frame-ancestors 'none'`, `X-Frame-Options: DENY` and, under D12 (b),
  `Cross-Origin-Opener-Policy: same-origin` [proposal; its effect checked by hand, rule 7];
  static assets from `ui_dir` (read-only). `ui_dir` unset → these routes 404. A path-traversal
  test.
- **Tests:**
  - Parity (S7): both runs (stand-ins and seams), the 2 confirm cases, the exclusion list,
    the outcome distribution; `scripts/parity_gate.py` as a new step in the `api` job, with its
    own tests (missing, doubled, skipped and wrongly-xfailed ids each fail); `freeze.json`
    unchanged.
  - PII at rest (S11): the 18 category E cases, each on its own session and prefixed with a
    red-flag stand-in term so it is stored, leave no `leak_forms` value in `message_masked`
    ("18 of 18" in the parity report); F's echoed-PII case; the widget route and a forced store
    failure in F's log path; an over-cap message stored as NULL.
  - Confirm through the widget [leo]: (1) a widget message returns `confirmation_required` with
    a 22-character token, not `**********` (the reveal is `ConfirmationResponse`'s JSON-mode
    serializer, `agent/responses.py:149-153`, so the wrapper must not `model_dump()` then
    re-serialize), and widget confirm executes once; a repeat returns the identical body with one
    write row; (2) two concurrent confirms: one write row, the same `ConfirmResponse`; (3) a body
    with `customer_ref` or any extra field → 422; a staff token as Bearer → 401; (4) for executed,
    409, 404, `confirm_failed`, `outcome_unknown`, `limits_unavailable` and 429, status, body and
    headers (except the trace id) equal `/v1/agent/actions/{id}/confirm`'s; (5) the token is in no
    log record, audit row, hand-off row, turn row or `agent_turn` record; (6) the route inventory:
    exactly `/v1/agent` and `/v1/widget/messages` can serialize `ConfirmationResponse`; (7) guards
    off → no service call.
  - S17: a flood of red-flag messages on one session → 1 row with the right `count`; normal then
    urgent → one urgent row; two concurrent "talk to a person" posts → one row, the same id.
  - Canary, under `tests/canary/`: each new route in `READ_PATHS`/`WRITE_PATHS`, with a red
    proof; session X cannot read, poll, rate or confirm session Y's hand-offs, turns and actions
    (extends `test_tool_customers.py`); framing headers per business.
  - Honesty (S8): `queued` true only when the row exists; a forced store failure keeps the
    guidance and gives `queued: false`; a hung store ends at its timeout.
  - S14: Redis down → an existing session's red-flag message is still urgent with guidance;
    session creation → typed 503; the session lookup with Postgres down → typed 503; the config
    route still answers with Postgres and Redis down.
  - Feedback (unless cut 4): another session's trace id, a made-up one and a second rating are
    refused.
- **Acceptance also shows:** the S13 diff-stat; `make check`; `make mcp-check`; `make
  test-integration`, then 0005.

## PR 3 — `m8/staff-api` (diego; ananya reviews extraction review, noah the labels and export, leo the actions view, zara the queue filters) · M · after PR 2, D14
- **Migration M8-c `extraction_reviews`:** `(business_id, id, extraction_id, field, decision ∈
  {confirmed, corrected, rejected}, value (only when corrected, ≤ 0007's bound), decided_by,
  decided_at)`, append-only: the latest row per field wins and an undo is a new row
  [proposal]. FK to `extraction_fields` with `ON DELETE CASCADE`, so the owner's purge still
  works; hidden when the parent extraction expires (0007's `EXISTS` pattern). SELECT and column
  INSERT only.
- **Routes** (cookie and role, S5; S5's CSRF dependency on every non-GET route;
  `Cache-Control: no-store`):
  - `GET /v1/staff/handoffs?status=&system=` (urgent → high → normal, then oldest; keyset
    paging), `GET /v1/staff/handoffs/{id}` (with that session's pending actions, read-only,
    joined by its `customer_ref`), `POST .../{id}/claim` (one atomic `UPDATE ... WHERE status =
    'open' RETURNING`; a race gets 409), `POST .../{id}/resolve {label?, reply?}`.
  - `GET /v1/staff/extractions?status=needs_review`, `GET .../{id}` (values and spans as text),
    `POST .../{id}/fields/{field}/decisions`.
  - `POST /v1/staff/extractions` (admin; a thin wrapper over `ExtractionGuard` and the service;
    cut 3).
- **System hand-offs, never hiding an emergency** [zara, noah]: each queue item carries
  `system: true` when its reason is in `INFRASTRUCTURE_HANDOFF_REASONS` (guard reasons
  included). One classification: a test pins the dashboard's set to that one, and the existing
  test already places every reason in exactly one of the two sets. **An urgent item is never
  hidden by any filter and never shown as plain "system":** the system filter applies to
  non-urgent items only. An urgent `guard_error` (the red-flag screen could not run,
  `guards/agent.py:552-563`) reads "Safety check could not run: treat as a possible emergency";
  any other urgent system item (today `safety_floor_off`) reads "Urgent: the automatic safety
  step did not clear this message". `system` still excludes them from precision.
- **Labels** [noah]: at resolve, a label is required on decision rows (`origin = turn`, reason in
  `DECISION_HANDOFF_REASONS`) and refused on system and button rows (the CHECK of PR 2). The
  metric, written now and computed in M11 (D10): "x needed of n labelled decision hand-offs with
  `origin = turn` (Wilson, `evalrun.intervals.wilson_interval`); unsure, unlabelled and
  expired-unresolved reported as counts beside it." With S17, n counts sessions' hand-offs, not
  messages, and the note says so.
- **The actions view** [leo]: per pending action, only `tool`, a derived state (`pending`,
  `expired`, `executed` or `failed`; expired is `expires_at <= now()` by the database clock, in
  the query), `created_at`, `expires_at`, `confirmed_at`, `finished_at`, `failure_reason`, the
  typed `result`, and the summary re-rendered by the tool's fixed template, with ticket text
  through S11's masking helper and rendered as text. Never `confirm_token_sha256` or raw
  `arguments` (model-written, untrusted, unmasked, W3).
- **Export (cut 5):** `python -m vectapilot_api.staff export-candidates --business <id>` writes
  JSONL into `evals/reports/tmp/`. Hand-off lines carry masked text only. Correction lines are
  written only when every row's business manifest says `"synthetic": true` and the environment is
  local, test or ci; otherwise the CLI refuses. Each line carries `business_id`, source table and
  id, commit, export time, `synthetic: true` and the labeller's role (never a name). The CLI
  hashes the `doc_extraction` dataset's document files without printing them and leaves out any
  extraction whose `file_sha256` matches, reporting the count (no test-split leak into a future
  dataset). Its tests use a temporary fixture directory; agents never open that dataset's
  `documents/`.
- **Purge:** `python -m vectapilot_api.staff purge-handoffs --business <id>` (owner role, by hand)
  NULLs the text columns of expired rows and keeps their metadata (S11).
- **Tests:** canary (staff A against B's hand-offs, actions, extractions and decisions; the queue
  and review queries in `OWNER_CONNECTION_PATHS`); reviewer on an admin route → 403; S5's
  dependency on every non-GET route (wrong or absent `Origin` → 403, a form or `text/plain` body
  → 4xx, no cookie → 401); the claim race; an urgent `guard_error` item visible under every
  `status` and `system` filter combination; the actions view has no `confirm_token_sha256` or
  `arguments` key; the route inventory finds no `/v1/staff/*` route depending on
  `get_confirm_service` or `ConfirmService` and no staff query writing to `pending_actions`; the
  label CHECK both ways; the export's refusals, provenance fields and hash exclusion; the purge
  keeps labels.
- **Acceptance also shows:** the S13 diff-stat; `make check`; `make test-integration`, then 0005.

## PR 4 — `m8/widget-ui` (sofia; zara reviews the hand-off and guidance states, leo the confirmation card) · L · starts after PR 1, merges after PR 2 · D2, D8, D9, D11
- **Packaging** [D2 (i)]: one Next app, `apps/web`, with the product pages built for the API's
  `ui_dir` only. The Pages artefact contains no widget or staff page that can call an API (a CI
  check on `out/`). Ids are read from the path or query in the client (static export: no
  request-time routing). Read `apps/web/AGENTS.md` and the installed Next docs first (rule 7).
- **States** (sofia's targets §2.2): greeting with "AI assistant" disclosure and an always-present
  "Talk to a person" in one place (3.2.6); the config route fetched here, its guidance kept in
  memory (S16); the session created lazily on the first send or "talk to a person" (S14);
  composing with a live counter at 2,000 code points (`Array.from(text).length`, S16) and no send
  during IME composition; one honest "working" status, no invented stage labels; `answer` with
  citation chips only under S9; `reply` with a provenance label; confirmation card (below);
  `handoff` (guidance first as `role="alert"` with `lang="en"`, then the follow-up text only when
  `queued` and not urgent; for urgent, D11's text or nothing, no polling, open cards dropped,
  S8); 429 with a `Retry-After` countdown; network error; client timeout; session expired; 422
  over length; "End chat" (revokes, clears, S18). Every non-success state shows the guidance
  first and keeps the typed message (S16). The widget never re-sends a message by itself; a
  manual retry after a timeout says "your last message may still be processed" (D15). Client
  timeouts for messages and confirm come from the session response, not hard-coded (P7).
- **The confirmation card** [leo]: summary in English with `lang="en"` and a label, expiry as
  text, Confirm and Not now, both disabled while a confirm is in flight, and a line saying only
  the Confirm button confirms (typing "yes" starts a new turn with no history, S13). Per response:

  | Response | Card says | Then |
  |---|---|---|
  | 200 | the fixed executed message | done |
  | 409 | the fixed failure sentence | done |
  | 404 (this session, this token) | "Expired, nothing was done" (provable: an executed action returns its stored result whatever its expiry, `actions/confirm.py:15-17`) | done |
  | 503 `confirm_failed` or `limits_unavailable` | "Not done yet" | retry with the same token |
  | 503 `outcome_unknown`, client timeout, network error | "We are checking"; never "nothing happened" | one automatic retry with the same token, then a manual retry with the same token until expiry, plus "Talk to a person" |
  | 429 | a countdown | the same token |
  | 401 | session expired; after an unknown outcome, it does not claim nothing was done | guidance (S16) |
- **i18n** [D9]: typed dictionaries `en`, `hi`, `te` for non-safety chrome; the AI disclosure,
  "Talk to a person", "not for emergencies" and the confirm card's safety lines come from the
  `en` dictionary with `lang="en"` and a label until a native reviewer signs off; follow-up texts
  come from business configuration per language; business text verbatim with its own `lang`; a
  completeness check (every key, same placeholders) run in the web job.
- **Tests:** `node --test` (Node 24's type stripping; verify on build day) for the state logic
  (one test per non-success state showing the guidance; one per confirm-card row; urgent
  hand-offs never render `handoff_follow_up`; Redis down at the first message shows guidance and
  contact fallback and no "a person will reply"), S9's invariant and its XSS fixtures, unknown
  kinds, the retry rule, a Telugu string near the 2,000-code-point bound, a storage spy (only the
  session-token key is ever written), and the i18n check, as a new step in `web (lint, build)`
  (job name unchanged); ESLint `react/no-danger: error` and `no-console` on the product pages;
  a pytest fixture guard in the `api` job validates every `apps/web` fixture against the API's
  models; S10's route-list test; under D8 (a), a new non-required `web e2e` job: every state at
  360×800 and 1280×800, keyboard only, reduced motion, en/hi/te, axe per state with "How we will
  know"'s pass rule (tags, zero violations, disabled rules listed in the PR), `retries: 0`, the
  API stubbed with `page.route` and a catch-all route that fails the test on any unstubbed request
  (so it stays at $0 and closed), and one e2e check that guidance shows on a network error.
- **Compose:** the built UI mounted read-only into `api` as `ui_dir` (takes effect at the next
  `make up`). A local demo host page frames the widget from an allowed origin.

## PR 5 — `m8/staff-ui` (sofia; ananya reviews extraction review, noah the labels) · M · starts after PR 2, merges after PR 3
- **Login** [D4 (a)]: the page reads the fragment, POSTs JSON, then clears the fragment from the
  address bar. Nothing to type (3.3.8).
- **Queue:** priority as text and shape, never colour alone; reason in plain words; a "system"
  label and filter for non-urgent items only, and PR 3's wording for urgent system items (never
  hidden, never plain "system"); a new urgent item announced (`aria-live="assertive"`) and counted
  in the title, both with priority and count only, never message text (S18); counts only, never
  rates (S19); polling (P7). **Detail:** masked message, the guidance shown, intent, `count`, a
  copyable `trace_id`, the session's actions read-only (PR 3's projection); claim and resolve by
  keyboard, with a label on decision rows only.
- **Extraction review:** value, quote span, page, level and reasons per field; accept, correct
  (validated per field, errors named in text) or reject; a standing notice that the document is
  not stored.
- **Tests:** as PR 4 (unit, fixture guard, lint rules, e2e under D8 (a) with the same pass
  rule), plus session expiry, a 409 claim, and an urgent item visible under every filter
  combination in the UI state logic.

## PR 6 — `m8/close-out` (shesha's steps; maya, sofia and diego help)
1. **Local smoke** (`make up`, fake provider): for each sample business, a session, a message
   that hands off (today an urgent `guard_error`, shown as "Safety check could not run"), "talk
   to a person" (the same row, S17), the item in that business's queue only, claim, resolve; one
   extraction decision on a fixture document from `apps/api/tests/` (never the `doc_extraction`
   dataset). Then the database back to 0005.
2. **Manual accessibility pass** per UI, recorded (S15).
3. **Bundle sizes:** gzipped JavaScript of the widget page and of the team-site home page, on
   `main` before PR 4 and after it, each with commit, `next` version and build command (M0's
   method).
4. **CI green:** canary gate, red-team gate, parity gate, `make mcp-check`, `web e2e` (if D8 (a)).
5. **The note** `docs/notes/m8-what-i-measured.md`: what was built and tested; what was measured
   (item 3, test counts, the parity and PII counts); "How we will know"'s §6 table; what the real
   stack cannot show yet and why (M4, M5 and M7 H1s, the router off); the per-business creation
   window as a lockout risk until M10; that precision waits for M11.
6. **Explain-backs**, the M8 study PDF and the quiz.
- **Done when** "How we will know M8 worked" holds, ADR 0019 is accepted and built, the site
  shows M8 with its pending note, and shesha has decided whether `web e2e` becomes a required
  check, from its run and flake counts across M8's PRs (a settings change, rule 1).

## Review notes (2026-11-24)
### Round 1: maya's reconciliation of sofia's and diego's targets
- **Route prefix:** diego's `/v1/widget/*` over sofia's `/v1/sessions` and `/v1/chat/*`: one
  prefix per surface, matching the page at `/widget/{business_id}`.
- **Tables:** diego's three migrations (about six tables) over sofia's nine. No conversation or
  message tables: the detail view is the session's hand-offs plus its pending actions, joined
  by `customer_ref` (D6 keeps the transcript as option (b)).
- **Lifetimes:** absolute only (diego) over absolute plus idle (sofia): an idle timer writes on
  every request. The missing staff idle timeout is listed in Risks.
- **Staff login:** diego's one-time links over sofia's scrypt passwords, as D4 (a): no password
  storage, lockout or reset flow in M8, and 3.3.8 holds by construction.
- **CORS:** off everywhere (diego) over configured origins (sofia): D2 (a) is same-origin.
- **CSRF:** SameSite=Strict, Origin and JSON-only. sofia's extra custom header is dropped as
  redundant: a cross-site JSON request needs a preflight, which fails with CORS off.
- **Extraction decisions:** sofia's append-only "latest wins" over diego's one immutable row per
  field: an undo is a new row, the grant stays INSERT-only, and the export keeps the history.
- **Labels:** set once at resolve, in the hand-off row (diego), over a labels table (sofia).
  "Answer was wrong" is dropped: answered turns are not stored under D6 (a).
- **System hand-offs:** tooleval's existing sets over a new list (one classification).
- **Security headers:** new routes and UI pages only (S13), not every response (diego's draft):
  `/v1/answer`'s and `/v1/kb/search`'s goldens record every response header in order
  (`tests/answer/goldens.py:9-13`), so a global header would change them inside M3's window.
- **`customer_ref`:** derived from the session uuid (diego) over a second random slug (sofia).
- **`[HAND-WRITE]`:** both proposed the label metrics; maya recommends none in M8 (D10).
- **Brand colour:** checked for contrast when the API loads the configuration, instead of a CI
  step over brand colours.

### Round 2: zara-safety, noah-evals and leo-agents (read-only, 2026-11-24, bbdf5c6 plus this draft)
Every finding is applied and none is rejected. The six that need shesha are decision items: D6
(sharpened) and D11–D15. Where two reviewers found the same gap, one fix covers both; where maya
changed a fix's form, the reason is given.
- **zara (7 major, 10 minor):**
  - Guidance only on hand-offs → S16 (every non-success state), PR 4's states and tests, and
    S14's typed 503 and store timeout.
  - An urgent hand-off followed by "a person will reply" → S8's rule; the wording is D11.
  - Urgent `guard_error` hidden as "system" → PR 3's "System hand-offs" and PR 5's queue.
  - Red-flag spam with no control → S17 (one open hand-off per session, per-session windows).
    This also covers leo's "talk to a person" idempotency.
  - The first text at rest without a PII test → S11's shared helper and the E and F replay
    (merged with noah's).
  - Vacuous, ungated parity → S7 and `scripts/parity_gate.py` (merged with noah's). Form
    changed: a sibling script instead of an extension of `redteam_gate.py`, which M7's close-out
    still runs with `--final`; the checks are the same.
  - Widget and staff on one origin → D12.
  - Minors: S6 (no unguarded branch; closed bodies, S1); S5 (one CSRF dependency, login included;
    atomic link use); S9 (CSP, `nosniff`, `no-referrer`, `no-store`, `react/no-danger`, XSS
    fixtures; no new package); S18 (what the browser keeps); S14 (lazy sessions; the lockout
    risk in Risks and the note); PR 1 (strict origins; a health business without guidance
    refused); feedback bound to turns (D6 (a)), else cut 4 first; S16's code-point counter; D9
    (safety strings in English); PR 3's export refusal.
  - Clarified: "no message text in live-region announcements" governs announcements (the
    dashboard's alert and title, the widget's status line). The widget's transcript stays a
    readable `role="log"`: it is the customer's own conversation and 4.1.3 needs it.
- **noah (7 major, 6 minor):**
  - "Repeat within 24 h" impossible with anonymous sessions → D6 (b)'s wrong claim removed,
    Out of scope, D13.
  - No denominator; unverifiable feedback → D6 sharpened with `widget_turns`.
  - The wrong-answer rate moved silently → a Deviation and D14.
  - A button press counted as an AI escalation → `origin`, the label CHECK and the metric's
    written definition (PR 2, PR 3).
  - Parity and the PII column → merged with zara's (S7, S11); exclusions are listed by id.
  - §6's metrics without a status → the table in "How we will know", the note, cut 4 reworded.
  - No accessibility gate → axe tags and pass rule, `retries: 0`, a catch-all route, and PR 6's
    decision on making `web e2e` required.
  - Minors: `channel` and `ui_language`; labels outlive retention (the purge keeps metadata, the
    first of noah's two options, so cut 5 loses no label); export provenance and the
    `doc_extraction` hash check (matching lines are left out and counted rather than marked with
    their split: simpler and fail-closed); the README and site wording (PR 0, with the main
    session) and S19; bundle sizes for both pages; D10's reason restated.
- **leo (4 major, 4 minor):**
  - The confirm flow through the wrapper untested → PR 2's seven tests; ADR 0019 names both W2
    routes.
  - Where the confirm token lives on the client → S18; `no-store` (S9).
  - The confirm card without per-response states → PR 4's table; buttons disabled in flight.
  - The staff view of pending actions without a field list → PR 3's projection and inventory
    tests.
  - Minors: "talk to a person" idempotency (S17); the widget route's bound → D15, with the
    no-automatic-resend rule applied in every option; MCP → ADR 0019 decision 9, `make
    mcp-check` in PR 1 and PR 2, and the `s-<32 hex>` refusal [proposal]; customer-scoped tools
    see only the session's own records → ADR 0019's Consequences.
- **Corrections by maya:** the token reveal is `agent/responses.py:149-153` (leo cited 183-187);
  `actions/confirm.py:15-17` is the "already done" rule.
- **Effect on scope:** no new PR. PR 2 and PR 4 grow from M to L, each with a named split point.
  No new package. The cut order gains one item (the `web e2e` matrix), and feedback moves to the
  front if D6's turn row is refused.

## Decisions for shesha (options and reasons; decided 2026-11-24, see "Decided" below)
- **D1 — Where sessions live.**
  - (a) **Postgres under RLS** (S3): durable, covered by the canary catalog, joins hand-offs,
    keeps Redis off the message path (M7 G1). One indexed read per request.
  - (b) Redis with a TTL: isolation in code only; no Redis volume in compose, so a restart logs
    everyone out; the message path would depend on Redis.
  - (c) Signed stateless tokens: no lookup, but a new server secret, and revocation still needs
    storage.

  **Recommendation: (a).**
- **D2 — Where the widget and dashboard run** (amends ADR 0002).
  - (a) **The API serves both static builds** from a read-only `ui_dir` (same origin). Locally a
    bind mount into the existing `api` container. Production hosting stays M10's. No new image,
    service or Node server. Cost: UI deploys tied to API deploys; static pages keep
    `'unsafe-inline'` scripts unless Next's experimental SRI is adopted.
  - (b) GitHub Pages plus the API elsewhere: cross-site, so no first-party staff cookie (tokens
    in script memory); Pages cannot send `frame-ancestors`, so Confirm and one-click decisions
    can be framed; a staff login page on a public site.
  - (c) A Next.js server (`output: "standalone"`): idiomatic server auth and nonce CSP, but a new
    Docker image on a tight disk, a second `node_modules` and a public Node server.
  - (d) Defer: local `next dev` only, no decision in M8.

  Packaging: (i) **one app, `apps/web`**, product pages built only for `ui_dir`; (ii) a second app
  `apps/console` (a second `node_modules`; the current one is 470 MB).
  **Recommendation: (a) with (i).** Nothing is publicly hosted in M8.
- **D3 — How the widget embeds, and where the customer token lives.**
  - (a) **An iframe page served by the API**, `frame-ancestors` per business, the bearer token in
    the iframe's memory and `sessionStorage`. Same-origin calls: no CORS, and CSRF does not apply
    to a header the browser never adds. Whether browsers partition the iframe's storage per top
    site is checked in PR 4 (rule 7).
  - (b) A script on the host page with per-business CORS: the host page can read the token.
  - (c) A partitioned third-party cookie (CHIPS): uneven support, and CSRF returns.

  **Recommendation: (a).** Embedding is one `<iframe>` line; no loader script in M8.
  `sessionStorage` holds the token alone; the transcript and any confirm token live in memory
  only (S18).
- **D4 — Staff login in M8.**
  - (a) **One-time links issued by the owner CLI** (15 minutes, single use, token in the URL
    fragment), each creating a new session; no passwords stored.
  - (b) Passwords with `hashlib.scrypt`, plus login limits, lockout and a reset flow.
  - (c) SSO: a package and external accounts.

  **Recommendation: (a);** production login is decided in M10. Cookie and roles as S5 in every
  option.
- **D5 — The `X-Business-Id` routes.**
  - (a) **Keep them as a local, test and ci harness seam**, never served in production (already
    enforced, now also tested by the route inventory).
  - (b) Drop `local` from the allowlist so local use goes through sessions: runbooks and the
    local smoke paths of M2–M7 would change, and the MCP server could no longer run locally (it
    imports `HEADER_ENVIRONMENTS`, `startup.py:30`).
  - (c) Remove them: impossible inside M3's window (the golden and the frozen suite use them).

  **Recommendation: (a);** revisit in M10.
- **D6 — What M8 stores, and for how long** (sharpened in round 2, noah and zara).
  - (a) **One hand-off row per session's open hand-off** (PR 2's table, S17: the message masked,
    the guidance shown, reason, origin, priority, intent, `trace_id`, status and label), **plus a
    metadata-only `widget_turns` row per turn** (kind, reason, priority, `trace_id`, latency, no
    text), and feedback bound to the session's own turns, one rating each. It gives "resolved"
    and the thumbs-up rate a denominator, "rated n of m turns", and M9 a per-conversation key.
    Retention per business (default 30 days, 1–90), hidden at expiry; the purge keeps metadata.
  - (b) Hand-off rows only; turn metadata as a `widget_turn` log record. Feedback cannot be
    verified (any session could rate any trace id), so it is not built: cut 4 moves to the front.
  - (c) (a) plus conversation and message tables: a full timeline, at more tables and more text
    at rest. It does **not** give §6's "repeat within 24 h" (an earlier draft said it did): it
    links turns inside one session only (D13).
  - (d) No text: staff see reason and priority only and cannot act.

  **Recommendation: (a).** One small table with no text buys every §6 denominator M11 needs.
  Synthetic data only until Q4c.
- **D7 — Can staff reply to the customer?**
  - (a) **A reply in the thread:** one reply text per hand-off; the widget polls the hand-off's
    status while open, never for an urgent hand-off (S8, D11). Without it an anonymous customer
    cannot hear back.
  - (b) None in M8: the widget shows the configured follow-up text and contact fallback.

  **Recommendation: (a), and first in the cut order** (falls back to (b)).
- **D8 — Frontend test packages** (rule 11; installing needs the network and a yes).
  - P1 `@playwright/test` (Microsoft; dev dependency; pulls `playwright-core`; Chromium is
    downloaded separately with `npx playwright install chromium`, its size measured on install
    day): real-browser keyboard, focus, viewport and reduced-motion tests with the API stubbed,
    at $0.
  - P2 `@axe-core/playwright` (Deque Systems; dev dependency; pulls `axe-core`): automated WCAG
    rule checks on every state.
  - (a) **Both, in a new non-required `web e2e` job** until shesha makes it required (a settings
    change, rule 1). (b) Neither: unit tests plus the manual pass only, so no automated
    accessibility floor. (c) P1 only.

  **Recommendation: (a)**, with the pass rule of "How we will know" (axe tags, zero violations,
  `retries: 0`, unstubbed requests fail). Not proposed: Vitest, Testing Library and jsdom (`node --test`
  covers the logic), an i18n library, `openapi-typescript` (the fixture guard instead),
  `argon2-cffi`, JWT or session libraries, `itsdangerous`, `python-multipart`. The backend needs
  no new package.
- **D9 — Languages of the UI text** (sharpened in round 2, zara).
  - (a) **en, hi and te for non-safety chrome**, hi and te drafted by Claude and labelled "not
    reviewed by a native speaker" until a named reviewer signs off. Safety-relevant strings (the
    AI disclosure, "Talk to a person", "not for emergencies", the confirm card's safety lines)
    stay English with `lang="en"` and a label until then; follow-up texts come from business
    configuration per language. Guidance is English with `lang="en"` until a reviewed localised
    safety file exists (Q8). Business text is shown verbatim and never translated by the client.
    Telugu messages to `paws-and-co` hand off (`not_covered_language`), and the Telugu UI says so.
  - (b) English UI text only in M8; hi and te when a reviewer exists.

  **Recommendation: (a).** No unreviewed translation carries a safety message.
- **D10 — `[HAND-WRITE]` in M8** (rule 2; propose, don't decide). Sessions, auth and UI are
  `[DELEGATE]` (not on rule 2's list). The only candidate is an eval metric: escalation
  precision from staff labels (PR 3's definition).
  - (a) **None in M8: deferred for workload, not for lack of data.** Four H1s are still open
    (M4, M5, M6, M7). M8 stores the labels (never cut). When the function is written (M11 by
    default), it is tested on hand-computed fixtures, as M2–M6's metric H2s were (for example 7
    needed of 9 labelled, 2 unsure, 3 system excluded), and reuses
    `evalrun.intervals.wilson_interval`; only its *result* waits for real labels. The learning
    goal is met by explain-backs on S3, S5, S14, S16, S17 and D2.
  - (b) shesha writes the metric in M8 (time-box 1 h, blocking no merge; a stub reports "not
    computed").
  - (c) Claude writes (b) at shesha's request, with an explain-back.

  **Recommendation: (a).**
- **D11 — What an urgent hand-off says after the guidance** (new in round 2, zara; feeds Q9).
  In every option the widget never shows the generic follow-up, an ETA or a reply prompt for an
  urgent or `safety` hand-off, and does not poll it (S8): both safety files say "Do not wait for
  a reply in the chat."
  - (a) Nothing after the guidance.
  - (b) **A separately configured `urgent_follow_up` per business and language** (for example
    "Staff have been alerted. Please follow the steps above now."), shown only when `queued`;
    nothing when it is absent.
  - (c) The generic `handoff_follow_up` ("a person will reply"): rejected by zara, it invites the
    waiting the guidance forbids.

  **Recommendation: (b);** shesha writes both sample businesses' texts with Q9.
- **D12 — Widget and staff on one origin?** (new in round 2, zara). Same-origin script defeats
  SameSite and the Origin check: one script injection in the widget page, opened top level by a
  logged-in staff member, could read that business's queue.
  - (a) Separate origins in M8: a `staff_origin` setting (locally a second host name or port);
    staff routes and `/staff/` served only on it, widget routes only on `public_origin`.
  - (b) **One origin while M8 is local and synthetic:** the widget page's `connect-src` limited to
    `<public_origin>/v1/widget/`, `Cross-Origin-Opener-Policy: same-origin` on `/staff/`
    [proposal, checked by hand], a runbook rule "never open the widget top level while logged in
    as staff", and separate origins a hard M10 requirement in ADR 0019's "Revisit when". It
    narrows the risk, it does not close it.

  **Recommendation: (b)** for pace, with the M10 requirement written down.
- **D13 — "Repeat within 24 h" with anonymous sessions** (new in round 2, noah). A session lives
  in one tab's `sessionStorage`; two visits by one customer are never linked, so §6's "resolved"
  and the auto-resolution rate cannot be measured from M8's data.
  - (a) **M8 builds nothing for repeats** and says so (Out of scope, the note). M11 picks (b) if
    Q4c allows it, otherwise (c).
  - (b) A pseudonymous returning-visitor id per business now: random, in the iframe's partitioned
    storage, hashed, kept 48 h or less. Device-level personal data before Q4c is answered.
  - (c) Redefine "repeat" as within one session, recorded as a deviation from §6 (signed off,
    Q11).

  **Recommendation: (a).** No personal data before Q4c and no change to a signed-off metric yet.
- **D14 — The wrong-answer rate** (new in round 2, noah). §6 places it in "Staff review (M8)".
  - (a) **Record a deviation: §6's row moves to M11**, which builds the collection when answers
    reach customers.
  - (b) Store answered turns that get a 👎 (masked answer text and source ids, hand-off
    retention), each a staff queue item labelled wrong or ok; needs D6 (a)'s turn row. In M8 it
    is proven on fixtures only.

  **Recommendation: (a).** No answered turn reaches the real stack in M8 (router off, H1s), so
  (b) would be built for fixtures. shesha changes §6's row.
- **D15 — A server-side bound on `/v1/widget/messages`** (new in round 2, leo; ADR 0016 and
  `config.py:26-43` left it open). `/v1/agent` has no outer timeout, and a `rag` route under
  `agent_router = on` can take about 151 s with the defaults (arithmetic in `config.py`), past
  the widget's 60 s. In every option the widget never re-sends a message by itself, and a manual
  retry after a timeout says "your last message may still be processed".
  - (a) **One outer bound at the new route** (a setting; default router + turn + margin), past
    it a `turn_timeout` hand-off (urgent with guidance for a health business); the client timeout
    is that bound plus a margin, read from the session response. A new route, outside the
    measured services: S13 holds.
  - (b) Keep the client's 60 s and document the gap until the router is wired.

  **Recommendation: (a).** A customer gets guidance instead of a spinner, and client and server
  agree on one number.

### Small proposals (applied unless shesha objects)
- **P1 Lifetimes:** customer session 12 h absolute (CHECK ≤ 24 h), staff session 8 h absolute,
  login link 15 minutes and single use; all set by the database clock.
- **P2 Roles:** `reviewer` (queue, extraction review) and `admin` (plus the staff upload).
- **P3 Cancel:** the widget drops the token; the action can never be confirmed and expires
  (900 s default, `config.py:224`). No route, no migration.
- **P4 Migration numbers:** the first of M8 and M7 PR 11 to merge takes 0008; the other
  renumbers before its merge (no merged migration is edited). M8 is expected first.
- **P5 Retention:** 30 days by default, 1–90 per business.
- **P6 Language of summaries:** English with a visible label and `lang="en"` (3.1.2).
- **P7 Timing:** client timeouts come from the session response. Messages: under D15 (a) the
  server bound is router + turn + margin = 15 + 35 + 10 = 60 s and the client waits the bound
  plus 5 s = 65 s; under D15 (b) the client waits 60 s (arithmetic over the defaults). Confirm:
  above the server's write backstop (`WRITE_TIMEOUT_S` 5 s, `tools/spec.py:44`), proposed 15 s.
  The hand-off store's own timeout (S14) is proposed at 2 s. Polling 5 s in the widget while a
  non-urgent hand-off is open (D7 (a)) and 10 s in the dashboard. Configuration, unmeasured; M9
  measures.

### Open questions (ask, don't invent)
1. **Q9, outside staff hours:** what should `handoff_follow_up`, and under D11 (b)
   `urgent_follow_up`, say for the two sample businesses? M8 reads both from configuration, so no
   code waits on the answer.
2. **Who reviews the hi and te UI text** (D9 (a))?
3. **Q12 retire-only** stays open with no approve screen in M8. Agreed?
4. **A demo business:** fixtures only (proposal) or a non-health synthetic business outside
   `data/sample/`? Either way the real stack shows only hand-offs until M5's and M7's H1s land.
5. **Production staff onboarding** (who issues links outside local): M10, agreed?
6. **Localised guidance** (business-context Q8, professional sign-off): until a reviewed hi or
   te safety file exists, guidance stays English with `lang="en"` in every UI language (D9).
   Agreed?

## Decided 2026-11-24 (shesha)
Every decision as recommended ("Accept all"); D8 and D14 were asked and answered separately.
"Decisions for shesha" above keeps the options and reasons. P1–P7 apply (no objection). The six
open questions stay open.
- **D1 (a):** sessions are rows in Postgres under RLS; only a token's SHA-256 is stored (S3).
- **D2 (a) with (i):** the API serves both static builds from a read-only `ui_dir` on its own
  origin, one Next app (`apps/web`); nothing is publicly hosted in M8; hosting is M10's.
- **D3 (a):** the widget is an iframe served by the API, `frame-ancestors` per business; the
  token in the iframe's memory and `sessionStorage`, nothing else stored there (S18).
- **D4 (a):** staff log in with one-time links from the owner CLI (15 minutes, single use, token
  in the fragment); production login is M10's.
- **D5 (a):** the 7 `X-Business-Id` routes stay a local, test and ci seam, proven unreachable in
  production by the route inventory; revisited in M10.
- **D6 (a):** one hand-off row per session's open hand-off, a metadata-only `widget_turns` row per
  turn, feedback bound to the session's own turns; retention 30 days by default (1–90).
- **D7 (a):** one staff reply per hand-off, polled only while a non-urgent hand-off is open;
  first in the cut order (falls back to (b)).
- **D8 (a)** ("Approve both"): `@playwright/test` and `@axe-core/playwright` as dev-only packages
  in a new non-required `web e2e` job; each install still needs its own yes, with the network, at
  its PR.
- **D9 (a):** en, hi and te for non-safety chrome, hi and te labelled unreviewed; safety strings
  and guidance stay English with `lang="en"`.
- **D10 (a):** no `[HAND-WRITE]` in M8; the labels are stored and the precision metric waits for
  M11.
- **D11 (b):** an optional `urgent_follow_up` per business and language, shown only when
  `queued`; shesha writes both sample texts with Q9.
- **D12 (b):** one origin while M8 is local and synthetic (`connect-src`, COOP, runbook rule);
  separate origins are a hard M10 requirement.
- **D13 (a):** M8 builds nothing for repeats and says so; M11 picks (b) if Q4c allows, else (c).
- **D14 (a)** ("Move it to M11"): §6's wrong-answer row moves from "Staff review (M8)" to M11, a
  recorded deviation from §6's 2026-10-13 sign-off.
- **D15 (a):** one outer bound on `/v1/widget/messages` (a setting, router + turn + margin), a
  `turn_timeout` hand-off past it, the client timeout from the session response.

## Risks
- **The real stack shows only hand-offs** (urgent `guard_error` for health businesses while M7's
  H1 is a stub; `safety_floor_off` after it; `agent_error` for other types until M5's H1). Other
  states are proven on fixtures only, and the urgent lane fills with system hand-offs in local
  use. Labelled in the queue ("Safety check could not run") and in the note; never hidden.
- **Parity proves little while H1 is a stub** (every case `guard_error` on both routes): the
  stand-in run and the outcome distribution are the control (S7).
- **Fixtures drift from the API:** the pytest fixture guard fails CI.
- **Cookie behaviour on plain http differs by browser** (`__Host-`, `Secure`): checked in PR 1;
  the fallback flag is refused in production.
- **XSS stays the main threat to a token in `sessionStorage`**, and static pages keep
  `'unsafe-inline'` scripts. Controls: text-only rendering enforced by lint and XSS fixtures, a
  strict CSP on the new pages, `no-store` (S9).
- **Widget and staff share one origin** under D12 (b): narrowed by `connect-src`, COOP and a
  runbook rule, not closed; separate origins are an M10 requirement.
- **Red-flag spam:** red-flag hits skip the limiter (M7 G1). One open hand-off per session (S17)
  bounds the queue by sessions; sessions are limited per business only until M10's edge limits.
- **Lockout by the per-business creation window:** one script can spend it and keep every new
  customer out until M10's IP limits. Lazy creation (S14) means page views do not spend it;
  locked-out customers still see the guidance and contact fallback. In the note.
- **Redis down blocks new sessions** (fail closed); the widget then shows the configured
  guidance and contact (S14, tested).
- **The 151 s `rag` path** under `agent_router = on` (`config.py:26-43`): D15.
- **Masked text limits staff:** a callback number reads `[PHONE]`; contact collection waits for
  Q4c.
- **One-time links are bearer credentials** printed in a terminal: 15 minutes, single use,
  local, test and ci only.
- **Staff login has no rate window in M8** (step B3, zara's review): the link's 256 random bits
  and single use are the control, and a per-business window keyed by a caller-written token
  prefix would only be a lockout switch. Per-IP limits wait for M10's edge.
- **No idle timeout on staff sessions** (8 h absolute): logout revokes; revisit in M10.
- **Migrations and the measurement database:** local integration runs must end at 0005, so every
  M8 downgrade is tested; numbering races M7 PR 11 (P4).
- **Disk:** 11 GiB free; Chromium for P1 adds an unmeasured amount; no new image.
- **hi and te strings are unreviewed** until someone reviews them (D9).
- **Debt:** four H1s, their explain-backs and M1–M7's live runs are open. M8 adds no hand-write
  under D10 (a), but adds explain-backs.

## If deadlines bite (cut in this order)
1. Staff reply in the thread and widget polling (D7 falls back to (b)).
2. Quick replies.
3. The admin extraction upload (the queue is seeded through the local header route).
4. 👍/👎 feedback. If cut, M11 must build feedback collection before its 2–4-week baseline.
   It moves to the front if D6 is decided without the turn row (unverifiable ratings would
   poison §6's "resolved").
5. The eval-candidate export (a deviation from "corrections become evals"; the rows stay stored,
   and the purge keeps labels, S11).
6. `cors_dev_origins` (sofia works against the API-served build).
7. The `web e2e` matrix down to 360×800 and en (axe on every state, keyboard and reduced motion
   stay; the manual pass is unchanged).
8. Extraction review routes and UI (the hand-off queue stays).

Never cut:
- sessions on every customer and staff surface, and no header route in production;
- the canary items, the route inventory and the session attack tests under `tests/canary/`;
- red-team parity on the widget routes with the stand-in run and its gate, and the PII-at-rest
  replay;
- no unguarded branch on a widget route;
- "talk to a person";
- guidance first, in every non-success state (S16); honest hand-off text; no "a person will
  reply" on an urgent hand-off (S8, D11);
- one open hand-off per session (S17);
- sources on every answer, and text-only rendering;
- the hand-off queue with priority, guidance shown, system labels that never hide an urgent item,
  and the escalation label;
- WCAG 2.2 AA basics and reduced motion;
- M3's window.

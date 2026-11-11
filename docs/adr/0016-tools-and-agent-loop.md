# ADR 0016 — Tools and the agent loop: a typed tool core, confirmation outside the loop, idempotent writes in Postgres

- **Status:** accepted · 2026-11-10, drafted by maya-tech-lead from leo-agents' playbook targets
  (`.claude/agents/leo-agents.md`), reviewed read-only the same day by leo-agents, zara-safety
  and diego-backend (findings applied; zara's blocker changed D7). shesha decided D1–D8 on
  2026-11-10, each as recommended (`docs/plans/m5-pr-checks.md`, "Decided 2026-11-10 (shesha)");
  parts marked "decided Dn" follow them, the rest are proposals. W1–W12 are that plan's
  write-safety rules. Nothing is built yet.

**Context.** The README names M5 "MCP tools + agent loop with confirmations". Leo's targets:
- an MCP server in `packages/mcp_server/` (the MCP Python SDK) with the tools `check_slots`,
  `book`, `reschedule`, `order_status` and `create_ticket`, each with a typed schema;
- least-privilege credentials, explicit confirmation before any write, idempotency keys,
  per-tool timeouts and an audit-log entry for every call;
- a sandbox backend (a fake clinic calendar and order store);
- tool-call evals: 95 %+ tool success and **zero** unconfirmed writes.

The agent loop is `[HAND-WRITE]` (rule 2). Business-context §3.4 says fast-changing facts (slots,
order status) should come from tools, not documents. §3.5 says a tool failure hands off with a
summary. §4.1/§4.2 and the §6 hard gate (100 % safety-escalation recall) apply to every route a
health business's customer can reach. §5 says each business's tools get credentials limited to
that business. Q10 (which systems each business type uses) is still open, so every real
integration is out of reach.

Eight constraints shape the choice:
1. **M3's measurement window** freezes `gateway/models.toml`, `PROMPT_VERSION`, `answer/prompt.py`
   and the gateway's output path, and `POST /v1/answer` stays as it is. The gateway sends text
   messages only (`gateway/types.py:315`) and no tools (`providers/anthropic.py:91`), so native
   tool use would change the output path. Its deadline is per instance (30 s by default,
   `config.py:102`).
2. **M4's router is offline.** `route_message` is shesha's open H1 (`intents/decide.py:152`), and
   its safety floor needs the classifier artifact, which needs live embeddings. ADR 0015
   decision 10 moved wiring to "M5's tool route, behind a setting".
3. **Both fictional businesses are health types** (`HEALTH_TYPES`, `intents/policy.py:65`).
4. **Paid calls are shesha's alone** (ADR 0007). M1–M4's live runs are all pending, so M5 must be
   built and tested at $0 with the fake provider.
5. **Isolation** (ADR 0004, 0009): every table with a `business_id` needs `FORCE` RLS, foreign
   keys into business tables pair `business_id` (`tests/canary/checks.py:170`), and the canary
   suite covers every new read and write path, also on the owner connection, where RLS never
   applies (the M3 PR 3 amendment).
6. **Rule 5:** tool outputs are untrusted data, never instructions.
7. **Dependencies:** classic `httpx` is gone from the API (`httpx2`, CLAUDE.md "Known issues"),
   and pytest turns warnings into errors (`pyproject.toml:99`). The MCP SDK's dependencies are to
   be confirmed with `uv lock` (rule 7).
8. **Disk:** the data volume is 98 % used (5.0 GiB free, 2026-11-10).

**Options.**
- MCP: the SDK in its own project as an adapter over an in-process tool core; a separate MCP server
  process with the API as its client; no SDK in M5 (decided D1 (a)).
- Tool choice by the model: structured output through `complete_structured`; native tool use
  (a gateway change); both (decided D2 (a)).
- Confirmation: a stored pending action confirmed by an explicit API call with a one-time token;
  "yes" in the chat; a signed, stateless token (decided D3 (a)).
- Idempotency: in Postgres, in the write's own transaction; Redis `SET NX`; a client
  `Idempotency-Key` header (decided D4 (a)).
- Sandbox: Postgres tables under RLS; an in-memory fake; a separate fake HTTP service
  (decided D6 (a)).
- Routing: the router behind a setting with health businesses failing closed while it is off;
  `off` sending everything to the loop; no router in M5 (decided D7 (a)).

**Decision.** One typed tool core, used in-process by an agent loop that can read and propose but
never write. Writes exist only as pending actions that a separate confirm call, carrying a token
the model never sees, executes exactly once. Each part:
1. **Tool core** (`vectapilot_api/tools/`, [DELEGATE]). One module per tool, each with a closed,
   bounded Pydantic input and output model (id patterns and lengths, a 14-day slot range, at most
   20 slots, enums from configuration), a kind (`read` or `write`) and a timeout. A registry lists
   the tools; each business's `data/sandbox/<business_id>/tools.json` names its enabled subset,
   service enum, business type (tested against the corpus manifest), emergency guidance, timezone
   and sandbox date (rule 9). It is not the corpus manifest, which is closed and hashed into
   freeze pins (`evalset/corpus.py:87`, `:201`). The API reads it from
   `VECTAPILOT_TOOLS_CONFIG_DIR` (a read-only bind mount under compose; the image is unchanged).
   The executor:
   - injects `business_id` and `customer_ref`; they are never schema fields (W7, tested);
   - runs read tools in a `READ ONLY` snapshot (`db.py:47`), with `SET LOCAL statement_timeout`
     and `lock_timeout` below the tool's budget and `asyncio.timeout` only as a backstop (W9);
   - caps each observation (rows and bytes) and returns an internal envelope (the rows'
     `business_id`s for the eval's leak check, the typed result for the prompt);
   - audits metadata only (W12): tool, kind, phase, outcome, duration, trace id, action id; no
     arguments hash, since low-entropy ids are reversible by enumeration.
2. **Write safety** (decided D3 (a)). The loop's tool runner has reads and `propose` only (W1; a
   test checks that only the confirm service imports the execute function).
   - The service stores a proposal as a pending action: validated arguments, `customer_ref`, a
     database-set `expires_at`, the hash of a 128-bit `confirm_token`, status.
   - The token is returned only in `/v1/agent`'s HTTP response, never in an observation, the
     prompt, the audit, logs or MCP results, so a model, or an MCP host with an HTTP tool, cannot
     confirm its own proposal (W2).
   - The summary renders typed fields only, never a free-text column; `create_ticket`'s text is
     shown quoted as the text that will be stored. Ticket text is untrusted data for every later
     consumer (M7, M8) (W3).
   - Only `POST /v1/agent/actions/{id}/confirm` with the token executes, with the stored
     arguments, and only it sets `confirmed_at`. Every write needs confirmation; chat text never
     confirms.
3. **Idempotency** (decided D4 (a)). The pending action's id is the idempotency key. Confirm, in
   READ COMMITTED, locks the action and the booking (`FOR UPDATE`), re-runs the full proposal
   validation, writes, inserts the `executed` audit row and marks the action `executed` with its
   result, in one transaction (W5).
   - Write tables carry `UNIQUE (business_id, action_id)`; a guarded reschedule
     (`... AND status = 'active'`, exactly one row) and a partial unique index on
     `retired_by_action_id` stop two actions from both moving one booking.
   - A repeated or concurrent confirm returns the stored result. A timed-out confirm is
     `outcome_unknown` to the caller, and a retry returns the stored result or executes once (W9).
   - Every refusal is the same 404 (W6).
4. **Model steps** (decided D2 (a)). Each step is one `complete_structured` call (plus at most one
   repair) with alias `small`, made by team code (`StepModel`), not by the loop. Its schema is
   `AgentStep`, built per business with `create_model` over the sorted enabled set: `call_tool`
   (one variant per enabled tool), `reply`, `handoff` (`customer_asked`, `safety`, `other`).
   - The prompt has its own version, `agent-step.1`, and its own fingerprint map, never
     `PROMPT_VERSION`. It states the business's sandbox date and timezone.
   - The customer's message and every observation sit in boundary blocks from
     `choose_boundary(texts, candidates=…)`, recomputed at every step over the whole prompt;
     `answer/prompt.py` is imported, never edited.
   - The agent has its own `LLMGateway` instance from the existing constructor, with its own
     attempt timeout and deadline, validated so that deadline × `max_steps` fits the turn budget.
     No change to `models.toml`, the gateway's code or `/v1/answer`.
5. **The loop** (H1, `[HAND-WRITE]`; decided D5 (b): shesha writes H1, Claude writes H2, the
   metrics, at shesha's request).
   `run_turn(message, *, step_model, tools, limits) -> TurnOutcome`; it never sees `business_id`.
   1. At most `max_steps` model steps (a configured default); at the cap, `step_cap`.
   2. `reply` and `handoff` end the turn.
   3. A read tool's typed result or typed error goes back as an observation.
   4. A write tool is `propose`d; a valid proposal ends the turn, a refusal is an observation.
   5. A second failure (`invalid_arguments`, `unavailable`, `timeout`) of one tool hands off
      with `tool_failure` (§3.5); `not_found` is a normal observation.
   6. `StructuredOutputInvalid` hands off with `invalid_model_output`, caught before any other
      `GatewayError` (`model_unavailable`), so the eval can tell a bad output from a down model.
   7. Any other exception from `tools` hands off (`tool_failure`), never a `reply`.
   8. `CancelledError` and the eval cap's `CapReached` propagate. The turn timeout belongs to the
      service (`turn_timeout`).
6. **Sandbox** (decided D6 (a)). Migration 0006 adds `sandbox_slots`, `sandbox_bookings`,
   `sandbox_orders`, `sandbox_tickets`, `pending_actions` and `tool_audit`.
   - `business_id` first in every key, `FORCE` RLS, ids unique per `(business_id, id)`.
     `pending_actions` adds `UNIQUE (business_id, id)` as the target of composite foreign keys
     from the write tables (bookings also to slots); `tool_audit.action_id` has no key.
   - The app role gets `SELECT` on slots and orders, `SELECT, INSERT, UPDATE` on bookings and
     pending actions, `SELECT, INSERT` on tickets and `tool_audit`. No `DELETE` anywhere.
   - Write rows carry `origin` (`seed` or `action`, with `action_id` set exactly for `action`), so
     seeded bookings never pass as executed writes; a booking retired by `reschedule` carries the
     retiring action's id.
   - Seeds are synthetic, per business, with two customers each and some instruction-like
     free text to test rule 5. The owner-role reset scopes every statement by `business_id`
     (the owner bypasses RLS) and never deletes `tool_audit`.
   - Real connectors later get per-business secret *names* in configuration and values from the
     process environment (the ADR 0007 pattern); the sandbox needs none.
7. **Routing and the safety step** (decided D7 (a)). `POST /v1/agent` is a new route; `/v1/answer`
   does not change. For a health business, no tool runs before a deterministic safety step (W11).
   `agent_router` (default `off`) puts M4's `route_message` in front: safety and other hand-off
   intents hand off before any tool, `action_request` reaches the loop only if the business enables
   tools, and `rag` routes call `AnswerService` (an `answer` response kind carrying its model
   unchanged). While it is off, or the live artifact is missing, health businesses fail closed with
   their configured emergency guidance. Every loop `handoff(safety)` becomes `URGENT` plus that
   guidance, never model text. The eval harness calls the service with `router=none`, which no route
   or setting can produce, and labels its report "safety floor not exercised". `intents/policy.py`
   does not change. [PR 4 review, 2026-11-11] The service does not trust a decision's route: it
   recomputes `policy.route(intent)` and fails closed (`router_failed`, never looser than the
   policy) on any disagreement, and checks mandatory intents before the loop and `rag`. `on`
   starts only with the model M4 pinned (the ledger's `PinEntry`: artifact sha256, T, tau, sigma,
   router prompt version), from a clean tree; the pin is what proves "committed" to an API image
   with no git. The `agent_turn` record names the router's artifact sha256.
8. **MCP** (decided D1 (a); rule 11 yes for the official `mcp` package, only in this project, its
   dependencies confirmed with `uv lock` on install day). `packages/mcp_server/` is its own uv
   project with its own lock (shared packages pinned to the API lock's versions, tested). It is a
   thin stdio adapter over the same registry, one process per business and customer
   (`--business-id`, `--customer-ref`, sandbox only), connecting only as `vectapilot_app`. Read
   tools run; write tools return the `propose` summary with no action id, no token and no pending
   action: MCP has no path to confirm in M5. The API never imports the SDK, so classic `httpx` stays
   out of its image. Its CI job runs against the same Postgres service as the `api` job.
9. **Customer identity.** `customer_ref` is a client-asserted body field (or the MCP flag),
   accepted only where `X-Business-Id` is (`business.py:24`: local, test, ci). Tools scope
   bookings and orders by (`business_id`, `customer_ref`) through the query clause, tested by a
   same-business customer canary; a foreign customer's id and an unknown id give identical
   errors. The IDOR (looking up another customer's order) is closed only when M8 authenticates
   the customer.
10. **Measurement** (decided D8 (b): at least 40 scenarios written by shesha, one live dev and one
    live test run with `small`, $1.00 in total). `tooleval` runs scripted scenarios against a seeded
    sandbox and counts from outside the code it checks: unconfirmed writes are action-origin or
    retired rows whose action was not confirmed through the route or not in the set the harness
    confirmed; duplicates are write rows beyond the expected count; the harness confirms only a
    proposal matching `expect`. Unrequested proposals and safety misses are counted by id. Tool
    success is `x of n` with a Wilson interval. Dry runs test plumbing only. Live runs are shesha's,
    each ≤ $1.00, counted by `count_spend` with M5's own `SpendRules`.
11. **Not in M5.** Native tool use, cancellations, refunds and payments, real integrations (Q10),
    server-side conversation history (M8's widget), confirming through MCP, the router on by
    default, guardrails on reply text and a rate limit (M7).

**Consequences.**
- **Zero unconfirmed writes is a property of the code, then a measurement.** The loop cannot reach
  a write, the token never reaches a model, and the database is counted against the harness's
  own record. A model that ignores instructions can propose a wrong write; it cannot execute one.
- **The route is closed for both fictional businesses until M4's router is live** (D7 (a)).
  The loop is measured through the harness, without the safety floor, and the note says so.
- **A health business with a corpus but no `tools.json` gets `not_configured` with no
  guidance**, because the guidance lives only in `tools.json`. Acceptable in M5 (both fictional
  businesses have both files; startup refuses a mismatch); revisit with the per-business
  configuration screen.
- **Under `on`, the router runs before the turn and outside its timeout**, so one request's worst
  case is the router's (embedding plus one fallback call) plus the turn's. Documented in
  `config.py`; a router timeout of its own is open for shesha.
- **Model replies are still model text.** A `reply` after a read can misstate a slot or claim a
  booking that never happened. The eval counts false success claims; M7 owns the guard.
- **New isolation surface.** Six tables, every read path (the read tools, the proposal checks)
  and every write path (`/v1/agent`, confirm) join the canary suite, on the app role and the
  owner connection, with bait ids in both businesses. Pending actions hold customer-related text:
  synthetic in M5; retention is an M9 decision.
- **Structured output instead of native tool use** (D2 (a)) costs one schema per business in
  the prompt (and in the cache key) and some loss of the provider's tool-use tuning. It is
  measured, not assumed, and revisited after the window.
- **A second Python project** (D1 (a)) means a second lock, a CI job with Postgres and a
  required check (a GitHub settings change: shesha's yes), and more disk.

**Revisit when:**
- M3's window closes: consider native tool use (D2 (c)) with a paired comparison on dev.
- M4 closes with a live artifact: measure `agent_router = on`, then consider it as the default.
- M8 authenticates customers: `customer_ref` from the session; a confirm path for MCP proposals.
- A real business names its systems (Q10): connectors, per-business credentials, and whether the
  MCP server becomes the API's only path to them.
- Any unconfirmed or duplicate write, or any safety miss, appears in any run: stop and fix
  before any other work.
- Pending actions or audit rows hold real data (M11): retention and masking (M7, M9).

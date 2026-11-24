# ADR 0019 — Server-side sessions per business, staff access, and the API serving the widget and dashboard

- **Status:** accepted · 2026-11-24 (D1–D15 decided by shesha, each as recommended, D8 and D14
  answered separately: `docs/plans/m8-pr-checks.md`, "Decided 2026-11-24 (shesha)"). Drafted the
  same day by maya-tech-lead from sofia-frontend's and diego-backend's read-only targets
  (bbdf5c6), reviewed read-only by zara-safety, noah-evals and leo-agents; findings applied (the
  plan's "Review notes", round 2). Parts marked "Dn" follow that decision; S1–S19 and P1–P7 are
  that plan's rules and small proposals. Nothing is built yet.

**Context.** M8 is the first milestone that faces a customer: the chat widget and the staff
review dashboard. Today nothing can be served to one.
- **Identity is client-asserted.** `X-Business-Id` names the business on 7 routes and is
  accepted only in local, test and ci (`business.py:24,38`), "until M8 auth replaces it"
  (ADR 0009). `customer_ref` is a body field (`agent/responses.py:112-118`), so ADR 0016's IDOR
  is open and M7's per-customer limit is "advisory" (`guards/limits.py:4-11`).
- **Where a UI can run.** ADR 0002 made the team site a static export on GitHub Pages and said
  the widget and dashboard "will need a server later (separate decision)". A static export has
  no cookies, request-time headers or server routes. GitHub Pages cannot send response headers,
  and `frame-ancestors` does not work from a `<meta>` tag.
- **What must not move.** M3's measurement window freezes the gateway, `PROMPT_VERSION`,
  `answer/prompt.py` and `/v1/answer`. Its goldens record every response header in order
  (`tests/answer/goldens.py:9-13`). The frozen red-team suite and 30 test files drive the header
  routes. M4–M7's pending runs go through the services (M7 G13).
- **Hard gates** (§6): zero cross-business leaks (RLS plus the canary suite, ADR 0004 and 0009)
  and 100 % safety-escalation recall (M7's frozen suite).
- **Constraints.** 11 GiB of free disk and `apps/web/node_modules` at 470 MB (2026-11-24): no new
  image or service without a decision. The API's lock has Starlette 1.7.0 (`StaticFiles`,
  `set_cookie` with `samesite`), `hashlib.scrypt` and `secrets`, but neither `itsdangerous` nor
  `python-multipart`. Rule 11 applies to every package.

**Options.**
- Session store (D1): Postgres under RLS; Redis with a TTL; signed stateless tokens (decided,
  D1 (a): Postgres).
- Where the UIs run (D2): the API serves the static builds from one origin; GitHub Pages plus a
  separate API; a Next.js server container; defer (decided, D2 (a) with (i): the API serves
  them, one app `apps/web`).
- Widget embedding and token (D3): an iframe served by the API with per-business
  `frame-ancestors` and a bearer token in the iframe; a host-page script with per-business CORS;
  a partitioned third-party cookie (decided, D3 (a): the iframe).
- Staff login (D4): one-time links from an owner CLI; passwords with `hashlib.scrypt`; SSO
  (decided, D4 (a): one-time links in M8; production login in M10).
- Staff and widget origins (D12): separate origins in M8; one origin while M8 is local and
  synthetic, narrowed, with separate origins required in M10 (decided, D12 (b): the second).
- The header routes (D5): keep as a local, test and ci seam; drop `local`; remove (decided,
  D5 (a): keep as a seam).

**Decision** (accepted 2026-11-24).
1. **Sessions are rows in Postgres under RLS** (D1 (a)). A table `sessions(business_id, id, kind ∈
   {customer, staff}, staff_id, token_sha256, created_at, expires_at, revoked_at)` follows
   0006/0007's isolation pattern (S12). The token is `<business_id>.<secrets.token_urlsafe(32)>`
   (256 random bits, arithmetic); only its SHA-256 is stored. The lookup parses the business
   prefix, opens that business's RLS transaction and reads by `(business_id, token_sha256)` with
   expiry and revocation in the query. A token whose prefix names another business is not found
   there, so no `SECURITY DEFINER` function and no `BYPASSRLS` is needed. Every failure is one
   fixed 401. Tokens, hashes, cookies and `Authorization` never reach a log line. Lifetimes are
   absolute and set by the database clock (P1: 12 h for customers, 8 h for staff).
2. **Every customer and staff route takes its identity from the session.** The business, and
   the customer or staff member, come from the session row, never from a header or a body. A
   customer's `customer_ref` is `s-` plus the session uuid's hex (34 characters, arithmetic),
   which fits the existing pattern and 0006's CHECKs. M7's per-customer window becomes per
   session with no limiter change, and ADR 0016's IDOR closes. Request bodies are closed
   (`extra="forbid"`). The new routes (`/v1/widget/*`) call the same guard, services and
   dependency functions as `/v1/agent` (M7 G13), with no unguarded branch; `/v1/agent` itself is
   unchanged. ADR 0016 W2's "one response that carries the confirm token" now names two:
   `/v1/agent` and `/v1/widget/messages`; no staff route can carry it.
3. **Staff** (D4 (a)) rows live in the database only, created by an owner CLI. In M8 they log in
   with a one-time link (15 minutes, single use by one atomic update, token in the URL fragment)
   that creates a new session. The session travels in a `__Host-` cookie: `HttpOnly; Secure;
   SameSite=Strict; Path=/`. CSRF is handled by SameSite=Strict plus one dependency on every
   non-GET staff route, login included: an `Origin` equal to the API's public origin (absent is
   refused), `Sec-Fetch-Site: same-origin` when sent, and JSON-only bodies. Roles are `reviewer`
   and `admin`, read on every request; a test maps every staff route to exactly one role. No
   cross-business staff access.
4. **The API serves both UIs as static builds from its own origin** (D2 (a) with (i)). A
   read-only `ui_dir` (a bind mount into the existing `api` container locally) holds the build of
   `apps/web`'s product pages. `/widget/{business_id}` carries `frame-ancestors` from that
   business's configured origins, and `/staff/` carries `frame-ancestors 'none'`. Calls are
   same-origin, so CORS stays off (one local-only development setting). Security headers go on
   the new routes and pages
   only, never on existing ones (M3's goldens). The Pages build keeps the team site and contains
   no page that can call an API. Production hosting is M10's decision. This amends ADR 0002 for
   the product UIs; the team site is unchanged. The new pages get a strict CSP, `nosniff` and
   `no-referrer`; every new API response gets `Cache-Control: no-store`. Staff and widget share
   that one origin in M8 (D12 (b)): the widget page may connect only to `/v1/widget/`, `/staff/`
   sends `Cross-Origin-Opener-Policy: same-origin`, and separate origins are an M10 requirement.
5. **The widget is an iframe** (D3 (a)) whose bearer token lives in the iframe's memory and
   `sessionStorage`, the only thing stored there; the transcript and any confirm token stay in
   memory. A browser never attaches the token on its own, so CSRF does not apply; the host page
   cannot read it, and the widget listens for no `postMessage`. Embedding is one `<iframe>` line,
   with no loader script in M8.
6. **`X-Business-Id` stays a test seam** (D5 (a)). Its 7 routes are unchanged and accepted only
   in local, test and ci. A route-inventory test under production settings proves that no
   reachable route depends on it, that every staff route has a role and that every customer
   route needs a session. A session route that receives the header answers 400. The MCP server is the other
   client-asserted surface: its startup refuses any environment outside local, test and ci
   (`packages/mcp_server/tests/test_startup.py`).
7. **Emergencies need no Redis.** A message on an existing session needs the API process and
   Postgres only (M7 G1). A session is created lazily, on the first message or "talk to a
   person". Creating one is rate-limited in Redis and fails closed; the widget then shows the
   business's configured guidance and contact from a files-only route, as it does in every other
   failure state.
8. **Isolation stays the gate.** The new tables join the canary catalog automatically. Every new
   route joins the read, write and owner-connection paths. Token-prefix tampering, a planted
   shared uuid and same-business sessions each get a test under `tests/canary/`. The frozen
   red-team agent and confirm cases replay through the widget routes with identical decisions,
   with M7's stand-in matchers and with the registered seams, gated from pytest's report.
9. **MCP has no session and nothing to confirm** (answers ADR 0016's revisit trigger). The MCP
   server stays flag-scoped and limited to local, test and ci; its proposals still create no
   pending action and carry no token. A confirm path for MCP proposals is deferred, with one
   constraint: the human who confirms does so in the widget, never in the MCP host. Its
   `--customer-ref` refuses the session form `s-<32 hex>` (a proposal, applied unless PR 1's
   review changes it).

**Alternatives rejected.**
- *Starlette's `SessionMiddleware`:* it needs `itsdangerous`, and it keeps state in a signed
  client cookie that cannot be revoked server-side.
- *JWTs* (`pyjwt`, `python-jose`): a signing secret to manage, and revocation needs storage
  anyway; opaque tokens are simpler to explain and test.
- *Sessions in Redis:* isolation in code only, lost on a restart (no volume), and the message
  path would depend on Redis.
- *Sessions on `/v1/agent` itself:* it changes the harness path during M5's and M7's pending
  runs and the frozen suite's route.
- *Passwords in M8:* storage, lockout and a reset flow for a local, synthetic dashboard.
- *GitHub Pages for the product UIs:* no first-party cookie, no `frame-ancestors`, and a staff
  login page on a public site.
- *A Next.js server:* a new image on a tight disk, a second `node_modules` and a public Node
  server, for features (nonce CSP, server rendering) M8 does not need.
- *Security headers on every response:* they would change `/v1/answer`'s recorded headers
  inside M3's window.

**Consequences.**
- **A customer session authenticates no person.** It binds one anonymous conversation to one
  business. Anyone can open one for an enabled business, as anyone can open its widget; the
  per-business creation window is the brake until M10's edge limits.
- **Customer-scoped tools see only what this session created.** Tools scope bookings and orders
  by `(business_id, customer_ref)` (ADR 0016 decision 9), so a fresh session never sees seeded
  customers: order lookups and reschedules return not-found unless the same session booked
  first. Linking a session to an existing customer needs verification (Q4c, M10) and is never
  made by the model or by a ref the customer types. Fixtures and any demo business follow this.
- **Identity can no longer be asserted** on any production route, and the IDOR closes. The header
  seam remains for tests and must never reach production (tested). Dropping it locally (D5 (b))
  would also stop the MCP server running locally, since it imports `HEADER_ENVIRONMENTS`.
- **One indexed Postgres read per request**, and no per-request write (no idle timeout). Staff
  sessions have no idle timeout in M8; logout revokes.
- **The UI ships with the API.** Deploys are coupled, and static pages keep `'unsafe-inline'`
  scripts unless Next's experimental SRI is adopted. Text-only rendering (enforced by lint and
  XSS fixtures) and the CSP are the XSS controls for a token in `sessionStorage`.
- **Cookie behaviour on plain http differs by browser**, so local development needs a flag that
  production refuses at startup.
- **Redis down means no new sessions**, never a missed emergency on an existing one.
- **No new runtime package, image or service.** The cost is three migrations and an `auth/`
  package of standard-library code that shesha must be able to explain. The only new packages in
  M8 are D8's two dev-only browser-test packages for the UIs (`@playwright/test`,
  `@axe-core/playwright`), each installed after its own yes.

**Revisit when:**
- M10 picks hosting, TLS and a domain: production `public_origin`, staff onboarding and login
  (passwords or SSO), idle timeouts, edge and IP limits, whether the header seam goes. Under
  D12 (b), **separate origins for staff and widget are a hard M10 requirement**, not an option.
- A confirm path for MCP proposals is needed: the confirming channel is the widget (decision 9).
- M3's window closes: security headers on every response; streaming.
- A business needs a loader script, or WhatsApp arrives (Q5): another session kind.
- Q4c is answered: real customer data, contact details from the session.
- Any canary or parity test fails: stop and fix isolation first.

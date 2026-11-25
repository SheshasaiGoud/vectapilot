/**
 * The closed stack the browser tests and the demo host page run on (M8 PR 4, step 4b). There is
 * no server and no open port: Playwright's routing answers every request the browser makes, from
 * files on disk, so nothing leaves the machine and nothing costs money.
 *
 * - **The API's origin** (`UI_ORIGIN`: compose's `VECTAPILOT_PUBLIC_ORIGIN`, PR 2) serves the
 *   product build (`out-ui/`) the way PR 2 is to serve its `ui_dir`: `/widget/{business_id}` is
 *   `widget.html` with S9's headers (CSP with `connect-src` narrowed to `/v1/widget/` under
 *   D12 (b), `frame-ancestors` from that business's `allowed_origins`, `nosniff`,
 *   `no-referrer`), and `/_next/...` are the build's files. `/v1/widget/*` is the caller's stub.
 * - **A business's allowed origin** serves the demo host page (`demo/host.html`), which frames the
 *   widget the way a business's own site would.
 * - **Anything else** is refused; the test fixture (`stack.ts`) records it and fails the test.
 *
 * Plain Node and no test runner here: `scripts/demo-host.ts` imports this module too (Node's type
 * stripping, so `import type` and `.ts` specifiers only).
 */

import { existsSync, readFileSync, readdirSync, realpathSync, statSync } from "node:fs";
import path from "node:path";

/** The API's origin in the local stack (`infra/compose.yaml`, PR 2's `VECTAPILOT_PUBLIC_ORIGIN`). */
export const UI_ORIGIN = "http://localhost:8000";
/** The only API prefix the widget page may connect to (D12 (b), S10). */
export const WIDGET_API_PREFIX = "/v1/widget/";

export interface Served {
  status: number;
  headers: Record<string, string>;
  body: string | Buffer;
}

const CONTENT_TYPES: Readonly<Record<string, string>> = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".ico": "image/x-icon",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
  ".woff2": "font/woff2",
};

const WIDGET_PAGE = /^\/widget\/([a-z0-9]+(?:-[a-z0-9]+)*)\/?$/;

/** S9's headers for the widget page, as the plan writes them (PR 2 sends the real ones). */
export function widgetPageHeaders(frameAncestors: readonly string[]): Record<string, string> {
  const ancestors = frameAncestors.length > 0 ? frameAncestors.join(" ") : "'none'";
  return {
    "content-type": CONTENT_TYPES[".html"],
    "content-security-policy": [
      "default-src 'none'",
      "script-src 'self' 'unsafe-inline'",
      "style-src 'self' 'unsafe-inline'",
      `connect-src ${UI_ORIGIN}${WIDGET_API_PREFIX}`,
      "img-src 'self' data:",
      "font-src 'self'",
      "base-uri 'none'",
      "form-action 'none'",
      `frame-ancestors ${ancestors}`,
    ].join("; "),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
  };
}

/** Each business's `allowed_origins`, read from `data/widget/businesses/` (the API's source). */
export function loadAllowedOrigins(repoRoot: string): ReadonlyMap<string, readonly string[]> {
  const folder = path.join(repoRoot, "data", "widget", "businesses");
  const origins = new Map<string, readonly string[]>();
  for (const name of readdirSync(folder)) {
    if (!name.endsWith(".json")) continue;
    const file = JSON.parse(readFileSync(path.join(folder, name), "utf8")) as {
      business_id: string;
      allowed_origins: string[];
    };
    origins.set(file.business_id, file.allowed_origins);
  }
  return origins;
}

/** The product build on disk, served as the API serves its `ui_dir`. */
export class UiFiles {
  readonly root: string;
  readonly #widgetHtml: Buffer;
  readonly #origins: ReadonlyMap<string, readonly string[]>;

  constructor(outUi: string, origins: ReadonlyMap<string, readonly string[]>) {
    const widget = path.join(outUi, "widget.html");
    if (!existsSync(widget)) {
      throw new Error(`${widget} is missing: run \`npm run build:ui\` in apps/web first`);
    }
    this.root = realpathSync(outUi);
    this.#widgetHtml = readFileSync(widget);
    this.#origins = origins;
  }

  /** The response for a path on the API's origin outside `/v1/`, or null when there is none. */
  serve(pathname: string): Served | null {
    const page = WIDGET_PAGE.exec(pathname);
    if (page !== null) {
      return {
        status: 200,
        headers: widgetPageHeaders(this.#origins.get(page[1]) ?? []),
        body: this.#widgetHtml,
      };
    }
    // Only the build's assets: never another page, never a path outside the folder.
    if (!pathname.startsWith("/_next/static/") && pathname !== "/icon.svg") return null;
    let file: string;
    try {
      file = realpathSync(path.join(this.root, decodeURIComponent(pathname)));
    } catch {
      return null;
    }
    if (!file.startsWith(this.root + path.sep) || !statSync(file).isFile()) return null;
    const type = CONTENT_TYPES[path.extname(file)];
    if (type === undefined) return null;
    return {
      status: 200,
      headers: { "content-type": type, "x-content-type-options": "nosniff" },
      body: readFileSync(file),
    };
  }
}

// ── Fixtures ────────────────────────────────────────────────────────────────────────────────────

/** One case of `fixtures/widget/<file>.json`: what the API answers, and the model it validates
 * against (`apps/api/tests/widget/test_web_fixtures.py`, in the api job). */
export interface FixtureCase {
  status: number;
  model: string;
  body: unknown;
  headers?: Record<string, string>;
  text?: string;
}

const fixtureFiles = new Map<string, Record<string, FixtureCase>>();

/** A copy of one case of `<webDir>/fixtures/widget/<file>.json`. */
export function readFixture(webDir: string, file: string, name: string): FixtureCase {
  const key = path.join(webDir, file);
  let cases = fixtureFiles.get(key);
  if (cases === undefined) {
    const json = readFileSync(path.join(webDir, "fixtures", "widget", `${file}.json`), "utf8");
    cases = (JSON.parse(json) as { cases: Record<string, FixtureCase> }).cases;
    fixtureFiles.set(key, cases);
  }
  const found = cases[name];
  if (found === undefined) throw new Error(`no fixture ${file}:${name}`);
  return structuredClone(found);
}

export interface BusinessContext {
  businessId: string;
  lang: string;
  guidance: readonly string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A fixture body as the API would send it to this business, with the same types: the session
 * token's `<business_id>.` prefix (S3) and `ui_language`, and the guidance list of a hand-off
 * turn or a 429 body (the business's own guard guidance, S8). Anything else unchanged. */
export function adaptBody(body: unknown, business: BusinessContext | null): unknown {
  if (business === null || !isRecord(body)) return body;
  const copy = structuredClone(body);
  if (typeof copy.token === "string" && copy.token_type === "bearer") {
    copy.token = `${business.businessId}.${copy.token.split(".").slice(1).join(".")}`;
    copy.ui_language = business.lang;
  }
  const turn = copy.turn;
  if (isRecord(turn) && turn.kind === "handoff" && Array.isArray(turn.guidance)) {
    if (turn.guidance.length > 0) turn.guidance = [...business.guidance];
  }
  if (copy.error === "rate_limited" && Array.isArray(copy.guidance)) {
    copy.guidance = [...business.guidance];
  }
  return copy;
}

/** The HTTP answer for a fixture case (an error status's body included). */
export function fixtureResponse(reply: FixtureCase, business: BusinessContext | null): Served {
  const text = reply.text ?? JSON.stringify(adaptBody(reply.body, business));
  return {
    status: reply.status,
    headers: {
      "content-type": reply.text !== undefined ? "text/plain; charset=utf-8" : "application/json",
      "cache-control": "no-store",
      ...reply.headers,
    },
    body: reply.status === 204 ? "" : text,
  };
}

// ── The demo host page ──────────────────────────────────────────────────────────────────────────

const ESCAPES: Readonly<Record<string, string>> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => ESCAPES[char]);
}

export interface HostPage {
  businessId: string;
  displayName: string;
  /** Where the iframe points: the API's origin (the real stack or this closed one). */
  uiOrigin: string;
  /** Shown on the page, so a screenshot can never pass for a real business's site. */
  note: string;
}

/** The demo host page (`demo/host.html`) for one business, every value escaped. */
export function renderHostPage(template: string, page: HostPage): string {
  const src = `${page.uiOrigin}/widget/${encodeURIComponent(page.businessId)}`;
  return template
    .replaceAll("{{display_name}}", escapeHtml(page.displayName))
    .replaceAll("{{widget_src}}", escapeHtml(src))
    .replaceAll("{{note}}", escapeHtml(page.note));
}

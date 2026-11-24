/**
 * The `stack` fixture of the browser tests (M8 PR 4, step 4b): the closed stack of `serve.ts`
 * plus the API's stubs, installed on the test's browser context before the page loads.
 *
 * The pass rule of the plan's "How we will know": any request that no stub or build file answers
 * fails the test (a catch-all route records and refuses it), so the run stays at $0 and closed.
 * The teardown also fails on an uncaught page error and on any console error the test did not
 * cause on purpose; Chromium reports every CSP violation there.
 *
 * Stubs answer with the fixtures of `fixtures/widget/` (the unit tests' fixtures; each case names
 * the API model it validates against). Three business-specific values are adapted to the
 * business under test, as the API would send them, with the same types: the session token's
 * `<business_id>.` prefix (S3) and `ui_language`, and the guidance list in a hand-off turn and in
 * a 429 body (the business's own guard guidance, S8).
 */

import path from "node:path";

import { test as base, expect } from "@playwright/test";
import type { BrowserContext, Page, Request, Route } from "@playwright/test";

import {
  UI_ORIGIN,
  WIDGET_API_PREFIX,
  UiFiles,
  adaptBody,
  fixtureResponse,
  loadAllowedOrigins,
  readFixture,
} from "./serve.ts";
import type { BusinessContext, FixtureCase, Served } from "./serve.ts";

export { adaptBody };
export type { BusinessContext, FixtureCase };

export const WEB_DIR = path.resolve(__dirname, "../..");
export const REPO_ROOT = path.resolve(WEB_DIR, "../..");
/** Every test starts at this instant (Playwright's clock), before every fixture's expiry. */
export const START_TIME = new Date("2026-11-24T12:00:00Z");

// ── Fixtures ────────────────────────────────────────────────────────────────────────────────────

/** A copy of one case of `fixtures/widget/<file>.json`. */
export function fixture(file: string, name: string): FixtureCase {
  return readFixture(WEB_DIR, file, name);
}

// ── Replies ─────────────────────────────────────────────────────────────────────────────────────

/** A reply held until the test releases it (the "working" state is on screen meanwhile); one
 * never released is no answer at all, and `arrived` says the request is out. */
export class Gate {
  readonly arrived: Promise<void>;
  readonly released: Promise<FixtureCase | "network">;
  #arrive!: () => void;
  #release!: (reply: FixtureCase | "network") => void;

  constructor() {
    this.arrived = new Promise((resolve) => (this.#arrive = resolve));
    this.released = new Promise((resolve) => (this.#release = resolve));
  }

  arrive(): void {
    this.#arrive();
  }

  release(reply: FixtureCase | "network"): void {
    this.#release(reply);
  }
}

/** A fixture case, a network error (`route.abort`), or a held reply. */
export type Reply = FixtureCase | "network" | Gate;

export interface Call {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
}

// ── The stack ───────────────────────────────────────────────────────────────────────────────────

let files: UiFiles | null = null;

function uiFiles(): UiFiles {
  files ??= new UiFiles(path.join(WEB_DIR, "out-ui"), loadAllowedOrigins(REPO_ROOT));
  return files;
}

const FAILED_STATUS = /^Failed to load resource: the server responded with a status of (\d{3})/;
const NETWORK_FAILED = /^Failed to load resource: net::ERR_FAILED/;

export class Stack {
  /** Every call to the API's origin under `/v1/`, in order. */
  readonly calls: Call[] = [];
  /** Requests nothing answered: each one fails the test (the stack is closed). */
  readonly unanswered: string[] = [];
  readonly pageErrors: string[] = [];
  readonly consoleErrors: string[] = [];
  business: BusinessContext | null = null;
  readonly #replies = new Map<string, Reply[]>();
  readonly #hosts = new Map<string, string>();
  readonly #servedStatuses = new Set<number>();
  readonly #allowedConsole: RegExp[] = [];
  #networkFailures = 0;

  /** The next replies for `METHOD /path`, in order; the last one repeats. */
  on(route: string, ...replies: Reply[]): this {
    this.#replies.set(route, [...(this.#replies.get(route) ?? []), ...replies]);
    return this;
  }

  /** Replaces every queued reply for `route`. */
  set(route: string, ...replies: Reply[]): this {
    this.#replies.set(route, [...replies]);
    return this;
  }

  callsTo(route: string): Call[] {
    return this.calls.filter((call) => `${call.method} ${call.path}` === route);
  }

  count(route: string | RegExp): number {
    return this.calls.filter((call) => {
      const key = `${call.method} ${call.path}`;
      return typeof route === "string" ? key === route : route.test(key);
    }).length;
  }

  /** Serves `html` as the top-level page of `origin` (the demo host page). */
  host(origin: string, html: string): this {
    this.#hosts.set(origin, html);
    return this;
  }

  /** A console error this test causes on purpose (say why at the call). */
  allowConsole(pattern: RegExp): this {
    this.#allowedConsole.push(pattern);
    return this;
  }

  async install(context: BrowserContext, page: Page): Promise<void> {
    uiFiles(); // fails early, with the fix, when the product build is missing
    page.on("pageerror", (error) => this.pageErrors.push(String(error)));
    page.on("console", (message) => {
      if (message.type() === "error") this.consoleErrors.push(message.text());
    });
    await context.route("**/*", (route) => this.#handle(route));
    await context.routeWebSocket(/.*/, (socket) => {
      this.unanswered.push(`WebSocket ${socket.url()}`);
      return socket.close();
    });
  }

  /** What the teardown checks: all three lists must be empty. */
  verdict(): { unanswered: string[]; pageErrors: string[]; consoleErrors: string[] } {
    const consoleErrors = this.consoleErrors.filter((text) => {
      // A stub's own error status and a stubbed network error are what the test asked for.
      const status = FAILED_STATUS.exec(text);
      if (status !== null && this.#servedStatuses.has(Number(status[1]))) return false;
      if (NETWORK_FAILED.test(text) && this.#networkFailures > 0) return false;
      return !this.#allowedConsole.some((pattern) => pattern.test(text));
    });
    return { unanswered: this.unanswered, pageErrors: this.pageErrors, consoleErrors };
  }

  async #handle(route: Route): Promise<void> {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === UI_ORIGIN && url.pathname.startsWith("/v1/")) {
      return this.#api(route, request, url);
    }
    if (url.origin === UI_ORIGIN) {
      const served = request.method() === "GET" ? uiFiles().serve(url.pathname) : null;
      if (served !== null) return fulfil(route, served);
      this.unanswered.push(`no build file for ${request.method()} ${url.pathname}`);
      return route.fulfill({ status: 404, body: "" });
    }
    const html = this.#hosts.get(url.origin);
    if (html !== undefined && url.pathname === "/" && request.method() === "GET") {
      return route.fulfill({
        status: 200,
        headers: { "content-type": "text/html; charset=utf-8" },
        body: html,
      });
    }
    this.unanswered.push(`${request.method()} ${url.href}`);
    return route.abort("blockedbyclient");
  }

  async #api(route: Route, request: Request, url: URL): Promise<void> {
    const raw = request.postData();
    let body: unknown = raw;
    if (raw !== null) {
      try {
        body = JSON.parse(raw);
      } catch {
        body = raw;
      }
    }
    const call: Call = {
      method: request.method(),
      path: `${url.pathname}${url.search}`,
      headers: await request.allHeaders(),
      body,
    };
    this.calls.push(call);
    const key = `${call.method} ${call.path}`;
    // S10: the widget calls only `/v1/widget/*`, never with a query.
    const queue = url.pathname.startsWith(WIDGET_API_PREFIX) ? this.#replies.get(key) : undefined;
    if (queue === undefined || queue.length === 0) {
      this.unanswered.push(`API call with no stub: ${key}`);
      return route.abort("blockedbyclient");
    }
    const reply = queue.length > 1 ? queue.shift()! : queue[0];
    return this.#answer(route, reply);
  }

  async #answer(route: Route, reply: Reply): Promise<void> {
    if (reply === "network") {
      this.#networkFailures += 1;
      return route.abort("failed");
    }
    if (reply instanceof Gate) {
      reply.arrive();
      const released = await reply.released;
      // The page may have given up meanwhile (its timeout): then there is nothing to answer.
      return this.#answer(route, released).catch(() => undefined);
    }
    this.#servedStatuses.add(reply.status);
    return fulfil(route, fixtureResponse(reply, this.business));
  }
}

function fulfil(route: Route, served: Served): Promise<void> {
  return route.fulfill({ status: served.status, headers: served.headers, body: served.body });
}

// ── The fixture ─────────────────────────────────────────────────────────────────────────────────

export const test = base.extend<{ stack: Stack }>({
  // `provide` is Playwright's `use` callback, renamed: React's hook lint reads `use` as a hook.
  stack: async ({ context, page }, provide) => {
    const stack = new Stack();
    await stack.install(context, page);
    // Every timer and `Date` in the page from a fixed instant: a countdown, a client timeout or
    // the 5 s poll is moved on with `page.clock.runFor`, never waited for.
    await page.clock.install({ time: START_TIME });
    await provide(stack);
    expect(stack.verdict(), "the closed stack: unanswered requests, page errors, console errors")
      .toEqual({ unanswered: [], pageErrors: [], consoleErrors: [] });
  },
});

export { expect };

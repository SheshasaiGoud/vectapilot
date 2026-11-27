/**
 * The demo host page, opened in a browser (M8 PR 4; ADR 0019 decision 5): a business's own site,
 * stood in for by `demo/host.html`, at that business's allowed origin
 * (`data/widget/businesses/<id>.json`), with the widget framed in it as a customer would see it.
 * The origin exists only inside this browser, so the widget's `frame-ancestors` lets it in and no
 * business configuration changes.
 *
 *   npm run build:ui
 *   npm run demo:host -- --business sunrise-clinic           fixtures: closed, $0, no stack
 *   npm run demo:host -- --business paws-and-co --stack      the local stack (make up; PR 2)
 *   npm run demo:host -- --business sunrise-clinic --check   headless: loads, checks, exits
 *
 * - **Fixtures** (the default): the browser answers every request itself, like the e2e tests:
 *   the product build from `out-ui/`, the API from `fixtures/widget/`. Every message gets today's
 *   real-stack answer for a health business, an urgent hand-off (M7's H1 is a stub).
 * - **--stack**: requests for the API's origin (`http://localhost:8000`) are passed to the local
 *   stack by Playwright, never by the page, and no redirect is followed. The API must serve
 *   `ui_dir` (PR 2) and run with `VECTAPILOT_PUBLIC_ORIGIN=http://localhost:8000`.
 *
 * In both modes every other request is refused and printed. Close the window to stop.
 */

import path from "node:path";
import { parseArgs } from "node:util";
import { readFileSync } from "node:fs";

import { chromium } from "@playwright/test";
import type { Route } from "@playwright/test";

import {
  UI_ORIGIN,
  UiFiles,
  fixtureResponse,
  loadAllowedOrigins,
  readFixture,
  renderHostPage,
} from "../e2e/support/serve.ts";
import type { BusinessContext, FixtureCase, Served } from "../e2e/support/serve.ts";

const WEB_DIR = path.resolve(import.meta.dirname, "..");
const REPO_ROOT = path.resolve(WEB_DIR, "../..");

const { values } = parseArgs({
  options: {
    business: { type: "string" },
    stack: { type: "boolean", default: false },
    api: { type: "string", default: UI_ORIGIN },
    check: { type: "boolean", default: false },
  },
});

const origins = loadAllowedOrigins(REPO_ROOT);
const businessId = values.business;
if (businessId === undefined || !origins.has(businessId)) {
  console.error(
    `--business is one of: ${[...origins.keys()].join(", ")} (data/widget/businesses/)`,
  );
  process.exit(2);
}
const hostOrigin = origins.get(businessId)?.[0];
if (hostOrigin === undefined) {
  console.error(`${businessId} has no allowed origin, so no page may frame its widget`);
  process.exit(2);
}

interface ConfigBody {
  display_name: string;
  languages: string[];
  guidance: string[];
}

const config = readFixture(WEB_DIR, "config", businessId);
const configBody = config.body as ConfigBody;
const business: BusinessContext = {
  businessId,
  lang: configBody.languages[0],
  guidance: configBody.guidance,
};
const mode = values.stack ? "the local stack" : "fixture data";
const html = renderHostPage(readFileSync(path.join(WEB_DIR, "demo", "host.html"), "utf8"), {
  businessId,
  displayName: configBody.display_name,
  uiOrigin: UI_ORIGIN,
  note: `Demo host page: a fictional business, not its real site. The chat uses ${mode}.`,
});

const fixture = (file: string, name: string): FixtureCase => readFixture(WEB_DIR, file, name);

/** Fixtures mode: what the API would answer on today's real stack (module docstring). */
function fixtureApi(method: string, pathname: string): Served {
  const route = `${method} ${pathname}`;
  const handoff = /^GET \/v1\/widget\/handoffs\/[0-9a-f-]{36}$/;
  let reply: FixtureCase;
  if (route === `GET /v1/widget/${businessId}/config`) reply = config;
  else if (route === "POST /v1/widget/sessions") reply = fixture("sessions", "created");
  else if (route === "POST /v1/widget/messages") reply = fixture("messages", "handoff-urgent-guard-error");
  else if (route === "POST /v1/widget/handoffs") reply = fixture("handoffs", "created");
  else if (handoff.test(route)) reply = fixture("handoff-status", "open");
  else if (route === "DELETE /v1/widget/session") reply = fixture("end-session", "revoked");
  else reply = { status: 404, model: "none", body: { detail: "not in the demo", error: "not_found" } };
  return fixtureResponse(reply, business);
}

const files = values.stack ? null : new UiFiles(path.join(WEB_DIR, "out-ui"), origins);

async function handle(route: Route): Promise<void> {
  const request = route.request();
  const url = new URL(request.url());
  if (url.origin === hostOrigin) {
    if (url.pathname === "/" && request.method() === "GET") {
      return route.fulfill({ status: 200, headers: { "content-type": "text/html; charset=utf-8" }, body: html });
    }
    return route.fulfill({ status: 404, body: "" }); // the browser's favicon request
  }
  if (url.origin === UI_ORIGIN && values.stack) {
    // Passed on by Playwright, so the page never connects to the local network itself.
    const response = await route.fetch({ url: `${values.api}${url.pathname}${url.search}`, maxRedirects: 0 });
    return route.fulfill({ response });
  }
  if (url.origin === UI_ORIGIN && files !== null) {
    if (url.pathname.startsWith("/v1/")) {
      const answer = fixtureApi(request.method(), url.pathname);
      return route.fulfill(answer);
    }
    const served = files.serve(url.pathname);
    if (served !== null) return route.fulfill(served);
  }
  console.error(`refused: ${request.method()} ${url.href}`);
  return route.abort("blockedbyclient");
}

const browser = await chromium.launch({ headless: values.check });
const context = await browser.newContext({ serviceWorkers: "block", viewport: null });
await context.route("**/*", handle);
const page = await context.newPage();
await page.goto(`${hostOrigin}/`);
console.log(`${hostOrigin}/ frames ${UI_ORIGIN}/widget/${businessId} (${mode})`);

if (values.check) {
  const frame = page.frameLocator("iframe");
  try {
    await frame.getByRole("heading", { level: 1, name: configBody.display_name }).waitFor({ timeout: 10_000 });
    console.log("check: the widget loaded inside the frame");
    await browser.close();
  } catch (error) {
    console.error(`check failed: ${String(error)}`);
    await browser.close();
    process.exit(1);
  }
} else {
  console.log("Close the browser window to stop.");
  await new Promise<void>((resolve) => browser.on("disconnected", () => resolve()));
}

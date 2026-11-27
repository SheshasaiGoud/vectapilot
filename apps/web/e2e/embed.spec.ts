/**
 * The widget framed by the demo host page (M8 PR 4; ADR 0019 decision 5, D3 (a), S9's
 * `frame-ancestors`). The host page is served at the business's allowed origin
 * (`data/widget/businesses/<id>.json`) inside the browser only, so these run at $0 and closed.
 */

import { readFileSync } from "node:fs";
import path from "node:path";

import type { Frame, Page } from "@playwright/test";

import { expectStateChecks, tabTo } from "./support/checks.ts";
import { UI_ORIGIN, loadAllowedOrigins, renderHostPage } from "./support/serve.ts";
import { REPO_ROOT, WEB_DIR, expect, fixture, test } from "./support/stack.ts";
import { LANGUAGES, Widget, messageReply } from "./support/widget.ts";

const TEMPLATE = readFileSync(path.join(WEB_DIR, "demo", "host.html"), "utf8");
const ORIGINS = loadAllowedOrigins(REPO_ROOT);
const NOTE = "Demo host page with fixture data: a fictional business, not its real site.";

/** S18: the host page listens for messages from the frame; the widget must post none (zara's
 * review: `parent.postMessage` is the vector an iframe would use). */
const LISTENER =
  "<script>window.__vpMessages = []; addEventListener('message', (event) => " +
  "window.__vpMessages.push(event.origin + ' ' + String(event.data)));</script>";

function withListener(html: string): string {
  if (!html.includes("<head>")) throw new Error("the host page has no <head>");
  return html.replace("<head>", `<head>${LISTENER}`);
}

async function messagesToHost(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as { __vpMessages: string[] }).__vpMessages);
}

/** One business per sample business, in its first (default) language. */
const BUSINESSES = [...new Set(LANGUAGES.map((language) => language.business))].map((business) => ({
  lang: "en" as const,
  business,
}));

async function widgetFrame(page: Page): Promise<Frame> {
  await expect(page.locator("iframe")).toBeVisible();
  await expect.poll(() => page.frames().some((frame) => frame.url().startsWith(`${UI_ORIGIN}/widget/`))).toBe(true);
  const frame = page.frames().find((candidate) => candidate.url().startsWith(`${UI_ORIGIN}/widget/`));
  if (frame === undefined) throw new Error("no widget frame");
  return frame;
}

for (const language of BUSINESSES) {
  test.describe(`embedded, ${language.business}`, () => {
    test("the demo host page frames the widget from an allowed origin", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      const allowed = ORIGINS.get(language.business)?.[0];
      if (allowed === undefined) throw new Error(`${language.business} has no allowed origin`);
      stack.host(allowed, withListener(renderHostPage(TEMPLATE, {
        businessId: language.business,
        displayName: w.config.display_name,
        uiOrigin: UI_ORIGIN,
        note: NOTE,
      })));
      stack.on("POST /v1/widget/messages", messageReply("handoff-urgent-guard-error"));
      w.prepare();
      await page.goto(`${allowed}/`);
      const frame = await widgetFrame(page);
      await expect(page.getByRole("heading", { level: 1, name: w.config.display_name })).toBeVisible();
      await expect(frame.getByText(w.texts.greeting, { exact: true })).toBeVisible();

      // Keyboard only, from the host page into the frame: today's real stack for a health
      // business (an urgent hand-off while M7's H1 is a stub).
      const box = frame.locator("#vp-message");
      await tabTo(page, box, { scope: frame });
      await page.keyboard.type(w.message);
      await page.keyboard.press("Enter");
      await expect(frame.locator("#vp-guidance")).toBeVisible();
      const [session] = stack.callsTo("POST /v1/widget/sessions");
      expect(session.headers.origin, "the widget's own origin, never the host's").toBe(UI_ORIGIN);
      expect(session.headers.referer).toBeUndefined();
      await expectStateChecks(page, { label: "embedded hand-off", guidance: w.guidance, urgent: true, scope: frame });
      expect(await messagesToHost(page), "S18: no postMessage to the host page").toEqual([]);
    });

    test("an origin that is not allowed cannot frame it", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      const stranger = "https://not-allowed.example";
      stack.host(stranger, renderHostPage(TEMPLATE, {
        businessId: language.business,
        displayName: w.config.display_name,
        uiOrigin: UI_ORIGIN,
        note: NOTE,
      }));
      // Chromium reports the refused frame on the console: that refusal is what this test wants.
      stack.allowConsole(/violates the following Content Security Policy directive: "frame-ancestors /);
      w.prepare();
      await page.goto(`${stranger}/`);
      await expect.poll(() => page.frames().some((frame) => frame.url().startsWith("chrome-error://"))).toBe(true);
      expect(stack.count(/^GET \/v1\/widget\//), "the widget never ran").toBe(0);
      expect(stack.consoleErrors.join("\n")).toContain("frame-ancestors");
    });

    test("D3: the framed widget's storage is partitioned by the top-level site (Chromium)", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      const allowed = ORIGINS.get(language.business)?.[0] ?? "";
      stack.host(allowed, withListener(renderHostPage(TEMPLATE, {
        businessId: language.business,
        displayName: w.config.display_name,
        uiOrigin: UI_ORIGIN,
        note: NOTE,
      })));
      stack.on("POST /v1/widget/handoffs", fixture("handoffs", "not-stored"));
      w.prepare();
      await page.goto(`${allowed}/`);
      const frame = await widgetFrame(page);
      const talk = frame.getByRole("button", { name: "Talk to a person", exact: true });
      await tabTo(page, talk, { scope: frame });
      await page.keyboard.press("Enter");
      await expect(frame.locator("#vp-guidance")).toBeVisible();
      const key = `vectapilot.widget.session.${language.business}`;
      expect(await frame.evaluate(() => Object.keys(window.sessionStorage))).toEqual([key]);
      expect(await messagesToHost(page), "S18: no postMessage to the host page").toEqual([]);

      // The same tab, the same origin, opened top level: another partition, so no token.
      await page.goto(w.url);
      await w.ready();
      expect(await page.evaluate(() => Object.keys(window.sessionStorage))).toEqual([]);
    });
  });
}

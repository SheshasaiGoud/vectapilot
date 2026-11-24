/**
 * What every state of the browser tests is held to (M8 PR 4, step 4b; S8, S15, S16).
 *
 * - **Keyboard only.** Tests reach a control with Tab (`tabTo`) and use it with Enter, never with
 *   the mouse. Every Tab stop on the way must show the focus ring (`:focus-visible`, at least a
 *   2 px outline) and be on screen (WCAG 2.4.7, 2.4.11).
 * - **axe** with the plan's pass rule: tags `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa`,
 *   `wcag22aa`; zero violations of any impact; every disabled rule listed with its reason
 *   (`DISABLED_RULES`, none today). Automated results are a floor, never a conformance claim.
 * - **Reflow:** no horizontal scrolling at 360 px (1.4.10).
 * - **Guidance** (S8, S16): when the state is not a success it is there, first, verbatim, a
 *   `role="alert"` with `lang="en"`, on screen, and never animated; when it is a success, absent.
 *   Its heading says whether it is an urgent hand-off's (zara's review).
 * - **What the browser keeps** (S18): `sessionStorage` holds at most the session token's key;
 *   `localStorage`, IndexedDB and Cache Storage hold nothing (zara's review).
 */

import AxeBuilder from "@axe-core/playwright";
import { expect } from "@playwright/test";
import type { Frame, Locator, Page } from "@playwright/test";

import { SAFETY } from "../../src/widget/i18n/safety.ts";
import { TOKEN_KEY_PREFIX } from "../../src/widget/core/storage.ts";

export const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] as const;

/** Rules turned off, each with its reason; the PR lists them. None. */
export const DISABLED_RULES: Readonly<Record<string, string>> = {};

type Scope = Page | Frame;

export async function expectNoAxeViolations(page: Page, label: string): Promise<void> {
  let builder = new AxeBuilder({ page }).withTags([...AXE_TAGS]);
  const disabled = Object.keys(DISABLED_RULES);
  if (disabled.length > 0) builder = builder.disableRules(disabled);
  const results = await builder.analyze();
  const found = results.violations.map(
    (violation) =>
      `${violation.id} (${violation.impact ?? "no impact"}): ` +
      violation.nodes.map((node) => node.target.join(" ")).join(" | "),
  );
  expect(found, `axe violations in ${label}`).toEqual([]);
}

interface FocusStop {
  name: string;
  ok: boolean;
  why: string;
}

/** The focused element in `scope` and whether its focus is visible and on screen. */
async function focusStop(scope: Scope): Promise<FocusStop | null> {
  return scope.evaluate(() => {
    const element = document.activeElement;
    if (element === null || element === document.body || element === document.documentElement) {
      return null;
    }
    const text = (element.getAttribute("aria-label") ?? element.textContent ?? "").trim();
    const name = `${element.tagName.toLowerCase()}${element.id ? `#${element.id}` : ""} "${text.slice(0, 40)}"`;
    if (element.tagName === "IFRAME") return { name, ok: true, why: "" }; // focus moves inside
    const style = getComputedStyle(element);
    const ring = style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 2;
    const box = element.getBoundingClientRect();
    const onScreen =
      box.bottom > 0 && box.right > 0 && box.top < window.innerHeight && box.left < window.innerWidth;
    const visible = element.matches(":focus-visible");
    const why = [!visible && "not :focus-visible", !ring && "no 2 px outline", !onScreen && "off screen"]
      .filter(Boolean)
      .join(", ");
    return { name, ok: why === "", why };
  });
}

/** Presses Tab (or Shift+Tab) until `target` has focus; every stop must show its focus. */
export async function tabTo(
  page: Page,
  target: Locator,
  { scope = page, max = 40, backwards = false }: { scope?: Scope; max?: number; backwards?: boolean } = {},
): Promise<void> {
  await expect(target).toBeVisible();
  await expect(target).toBeEnabled();
  for (let presses = 0; presses <= max; presses += 1) {
    if (await target.evaluate((element) => element === element.ownerDocument.activeElement)) return;
    await page.keyboard.press(backwards ? "Shift+Tab" : "Tab");
    const stop = await focusStop(scope);
    if (stop !== null && !stop.ok) throw new Error(`focus not visible on ${stop.name}: ${stop.why}`);
  }
  throw new Error(`Tab did not reach the target in ${max} presses`);
}

/** Tab to `target`, then Enter: how a keyboard user presses a button or opens a citation. */
export async function press(page: Page, target: Locator, scope: Scope = page): Promise<void> {
  await tabTo(page, target, { scope });
  await page.keyboard.press("Enter");
}

/** Tabs through the whole page once: each stop, and each stop that hides its focus. */
export async function walkFocus(
  page: Page,
  scope: Scope = page,
  max = 60,
): Promise<{ stops: string[]; problems: string[] }> {
  const stops: string[] = [];
  const problems: string[] = [];
  let empty = 0;
  for (let presses = 0; presses < max; presses += 1) {
    await page.keyboard.press("Tab");
    const stop = await focusStop(scope);
    if (stop === null) {
      empty += 1;
      if (empty > 2) break;
      continue;
    }
    if (stops.includes(stop.name)) break; // round the page once
    stops.push(stop.name);
    if (!stop.ok) problems.push(`${stop.name}: ${stop.why}`);
  }
  return { stops, problems };
}

export async function horizontalOverflow(scope: Scope): Promise<number> {
  return scope.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

/** S8, S16: the guidance's place, language, heading, text and stillness, or its absence. */
export async function expectGuidance(
  scope: Scope,
  lines: readonly string[] | null,
  { urgent = false }: { urgent?: boolean } = {},
): Promise<void> {
  const guidance = scope.locator("#vp-guidance");
  if (lines === null || lines.length === 0) {
    await expect(guidance).toHaveCount(0);
    return;
  }
  await expect(guidance).toBeVisible();
  await expect(guidance).toHaveAttribute("role", "alert");
  await expect(guidance).toHaveAttribute("lang", "en");
  await expect(guidance.locator("h2")).toHaveText(
    urgent ? SAFETY.guidanceHeadingUrgent : SAFETY.guidanceHeading,
  );
  await expect(guidance.locator("p")).toHaveText([...lines]);
  const facts = await guidance.evaluate((element) => {
    // The chat's status region (always there while chatting); the "unavailable" and "ended"
    // views have none, and there the guidance only has to come first in its area.
    const status = document.querySelector("main div[role='status']");
    const box = element.getBoundingClientRect();
    return {
      // First in its area: before the status line and the contact fallback when they exist.
      first:
        element.previousElementSibling === null &&
        (status === null ||
          (element.compareDocumentPosition(status) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0),
      animations: element.getAnimations({ subtree: true }).length,
      animationName: getComputedStyle(element).animationName,
      topOnScreen: box.top >= 0 && box.top < window.innerHeight,
    };
  });
  expect(facts, "guidance first, still and on screen").toEqual({
    first: true,
    animations: 0,
    animationName: "none",
    topOnScreen: true,
  });
}

/** S18: what the page has stored, beyond the session token's one key (none of it allowed). */
export async function storedBeyondToken(scope: Scope): Promise<Record<string, unknown>> {
  return scope.evaluate(async (prefix) => {
    const session = Object.keys(window.sessionStorage).filter((key) => !key.startsWith(prefix));
    const databases = typeof indexedDB === "undefined" ? [] : await indexedDB.databases();
    const caches_ = typeof caches === "undefined" ? [] : await caches.keys();
    return {
      sessionStorage: session,
      sessionKeys: Object.keys(window.sessionStorage).length,
      localStorage: window.localStorage.length,
      indexedDB: databases.length,
      cacheStorage: caches_.length,
    };
  }, TOKEN_KEY_PREFIX);
}

/** The checks every state ends with (module docstring); focus moves, so it runs last. */
export async function expectStateChecks(
  page: Page,
  {
    label,
    guidance,
    urgent = false,
    scope = page,
  }: { label: string; guidance: readonly string[] | null; urgent?: boolean; scope?: Scope },
): Promise<void> {
  await expectGuidance(scope, guidance, { urgent });
  const stored = await storedBeyondToken(scope);
  expect(stored, `S18: nothing stored but the session token in ${label}`).toEqual({
    sessionStorage: [],
    sessionKeys: expect.any(Number),
    localStorage: 0,
    indexedDB: 0,
    cacheStorage: 0,
  });
  expect(stored.sessionKeys as number, "at most the token's one key").toBeLessThanOrEqual(1);
  expect(await horizontalOverflow(scope), `horizontal overflow in ${label}`).toBeLessThanOrEqual(0);
  await expectNoAxeViolations(page, label);
  const { stops, problems } = await walkFocus(page, scope);
  expect(stops.length, `Tab reaches the controls in ${label}`).toBeGreaterThan(0);
  expect(problems, `focus visible at every Tab stop in ${label}`).toEqual([]);
}

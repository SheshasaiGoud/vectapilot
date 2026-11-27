/**
 * The harness proves it can fail (M8 PR 4, step 4b): each check of `support/` is made to see the
 * fault it exists for, on purpose, as M0 proved the team site's drift guard. A check that cannot
 * fail would pass every state above without meaning anything.
 */

import { expectGuidance, expectNoAxeViolations, tabTo, walkFocus } from "./support/checks.ts";
import { UI_ORIGIN } from "./support/serve.ts";
import { expect, test } from "./support/stack.ts";
import { LANGUAGES, Widget } from "./support/widget.ts";

const EN = LANGUAGES[0];

test.describe("the harness fails when it should", () => {
  test("a request nothing answers is recorded (the catch-all)", async ({ page, stack }) => {
    stack.host("https://host.example", "<!doctype html><html lang='en'><title>t</title><h1>t</h1>");
    await page.goto("https://host.example/");
    await page.evaluate(() => fetch("https://unstubbed.example/pixel").catch(() => null));
    expect(stack.verdict().unanswered).toEqual(["GET https://unstubbed.example/pixel"]);
    stack.unanswered.length = 0; // seen: the teardown checks the rest
    stack.consoleErrors.length = 0;
  });

  test("an API call with no stub is recorded, and nothing is sent", async ({ page, stack }) => {
    const w = new Widget(page, stack, EN);
    await w.open();
    await w.send();
    await expect(page.locator("#vp-guidance")).toBeVisible(); // to the widget: a network error
    expect(stack.verdict().unanswered).toEqual(["API call with no stub: POST /v1/widget/messages"]);
    stack.unanswered.length = 0;
    stack.consoleErrors.length = 0;
  });

  test("the page's CSP refuses any API path outside /v1/widget/ (S10, D12 (b))", async ({ page, stack }) => {
    const w = new Widget(page, stack, EN);
    await w.open();
    await w.ready();
    const refused = await page.evaluate(
      (url) => fetch(url).then(() => "fetched", () => "refused"),
      `${UI_ORIGIN}/v1/answer`,
    );
    expect(refused).toBe("refused");
    expect(stack.calls, "it never left the page").toEqual([expect.objectContaining({ path: `/v1/widget/${EN.business}/config` })]);
    expect(stack.verdict().consoleErrors.join("\n")).toContain("Content Security Policy");
    stack.consoleErrors.length = 0;
  });

  test("axe reports a violation", async ({ page, stack }) => {
    const w = new Widget(page, stack, EN);
    await w.open();
    await w.ready();
    await page.evaluate(() => document.body.append(Object.assign(document.createElement("img"), { src: "data:," })));
    await expect(expectNoAxeViolations(page, "an image without alt text")).rejects.toThrow(/image-alt/);
  });

  test("a hidden focus ring is reported", async ({ page, stack }) => {
    const w = new Widget(page, stack, EN);
    await w.open();
    await w.ready();
    await page.addStyleTag({ content: ":focus-visible { outline: none !important; }" });
    const { problems } = await walkFocus(page);
    expect(problems.join("\n")).toContain("no 2 px outline");
    await expect(tabTo(page, w.box)).rejects.toThrow(/focus not visible/);
  });

  test("animated or relabelled guidance is reported", async ({ page, stack }) => {
    const w = new Widget(page, stack, EN);
    stack.on("POST /v1/widget/messages", "network");
    await w.open();
    await w.send();
    await expectGuidance(page, w.guidance);
    await page.addStyleTag({
      content: "@keyframes vp-x { from { opacity: 0.5; } to { opacity: 1; } } #vp-guidance { animation: vp-x 1s infinite; }",
    });
    await expect(expectGuidance(page, w.guidance)).rejects.toThrow();
    await page.addStyleTag({ content: "#vp-guidance { animation: none !important; }" });
    await expectGuidance(page, w.guidance);
    await page.locator("#vp-guidance").evaluate((element) => element.setAttribute("lang", "hi"));
    await expect(expectGuidance(page, w.guidance)).rejects.toThrow();
  });
});

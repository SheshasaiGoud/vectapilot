/**
 * Motion in both preferences (M8 PR 4; S15): the one animation, the "working" dots, explains that
 * a request is in flight and runs only without reduced motion; the guidance never animates in
 * either mode. The other specs run under `reduce` only (`playwright.config.ts`).
 */

import { expectGuidance } from "./support/checks.ts";
import { Gate, expect, test } from "./support/stack.ts";
import { LANGUAGES, Widget } from "./support/widget.ts";

for (const preference of ["reduce", "no-preference"] as const) {
  test.describe(`prefers-reduced-motion: ${preference}`, () => {
    test.use({ reducedMotion: preference });

    test("the working dots move only without reduced motion; the guidance never does", async ({ page, stack }) => {
      const w = new Widget(page, stack, LANGUAGES[0]);
      const gate = new Gate();
      stack.on("POST /v1/widget/messages", gate);
      await w.open();
      await w.send();
      await gate.arrived;
      await expect(w.status).toContainText(w.strings.working);
      const animated = await page.evaluate(() =>
        document.getAnimations().map((animation) => {
          const target = (animation.effect as KeyframeEffect | null)?.target;
          return target instanceof Element ? target.className : "?";
        }),
      );
      expect(animated).toEqual(preference === "reduce" ? [] : ["vp-dot", "vp-dot", "vp-dot"]);

      gate.release("network");
      await expect(page.locator("#vp-guidance")).toBeVisible();
      await expectGuidance(page, w.guidance);
      expect(await page.evaluate(() => document.getAnimations().length), "nothing moves").toBe(0);
    });
  });
}

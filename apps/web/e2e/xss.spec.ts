/**
 * S9 in a real browser, under the widget page's CSP (zara's review): every place untrusted text
 * reaches the widget carries payloads that would set a canary if any of them ran. The unit tests
 * check the server-rendered markup; this checks the live DOM after React and Chromium had their
 * turn. A created `<img src=x>` would also request `/widget/x`, which the closed stack refuses and
 * the teardown reports.
 */

import { SAFETY } from "../src/widget/i18n/safety.ts";
import { expectStateChecks, horizontalOverflow, press } from "./support/checks.ts";
import { expect, fixture, test } from "./support/stack.ts";
import { LANGUAGES, Widget, messageReply } from "./support/widget.ts";

const MESSAGES = "POST /v1/widget/messages";
const POLL = "GET /v1/widget/handoffs/6f1c2a9e-0d4b-4c8e-9a51-2b7e3f4a5c61";

const PAYLOADS = [
  `<img src=x onerror="window.__pwned=1">`,
  `<svg onload="window.__pwned=1"></svg>`,
  `<script>window.__pwned=1</script>`,
  `"><img src=x onerror=window.__pwned=1>`,
  `javascript:window.__pwned=1`,
  `<a href="javascript:window.__pwned=1">link</a>`,
].join(" ");
const payload = (where: string): string => `${where}: ${PAYLOADS}`;

interface Answer {
  turn: { answer: { answer: string; sources: { title: string; content: string }[] } };
}

test("no payload runs, wherever untrusted text is shown", async ({ page, stack }) => {
  const dialogs: string[] = [];
  page.on("dialog", (dialog) => {
    dialogs.push(dialog.message());
    void dialog.dismiss();
  });
  const w = new Widget(page, stack, LANGUAGES[0], "sunrise-clinic-follow-ups");
  // The business's configuration (S8's guidance included: adaptBody copies it into hand-offs).
  w.config.display_name = payload("name");
  w.config.guidance = [payload("guidance one"), payload("guidance two")];
  Object.assign(w.texts, {
    greeting: payload("greeting"),
    quick_replies: [payload("quick reply")],
    contact_fallback: [payload("contact")],
    handoff_follow_up: payload("follow-up"),
    urgent_follow_up: payload("urgent follow-up"),
  });
  // Model text, a citation, a tool summary and a staff reply.
  const answer = messageReply("answer");
  const cited = (answer.body as Answer).turn.answer;
  cited.answer = payload("answer");
  cited.sources[0].title = payload("citation title");
  cited.sources[0].content = payload("citation excerpt");
  const reply = messageReply("reply");
  (reply.body as { turn: { text: string } }).turn.text = payload("reply");
  const proposal = messageReply("confirmation-book");
  (proposal.body as { turn: { summary: string } }).turn.summary = payload("card summary");
  const staff = fixture("handoff-status", "replied");
  (staff.body as { reply: string }).reply = payload("staff reply");
  stack.on(MESSAGES, answer, reply, proposal, messageReply("handoff-normal-queued"), messageReply("handoff-urgent-guard-error"));
  stack.on(POLL, staff);

  await w.open();
  await w.ready();
  await expect(w.text(payload("greeting"))).toBeVisible();
  // Long unbroken untrusted text still reflows at 360 px (WCAG 1.4.10): checked here for the
  // greeting and its quick replies, and at the end for everything else.
  expect(await horizontalOverflow(page), "reflow with the greeting's payloads").toBeLessThanOrEqual(0);
  await w.send("one");
  await expect(w.text(payload("answer"))).toBeVisible();
  await press(page, page.locator("summary").first());
  await expect(w.text(payload("citation excerpt"))).toBeVisible();
  await w.send("two");
  await expect(w.text(payload("reply"))).toBeVisible();
  await w.send("three");
  await expect(w.text(payload("card summary"))).toBeVisible();
  await w.send("four");
  await expect(w.text(payload("follow-up"))).toBeVisible();
  await expect(w.text(payload("contact"))).toBeVisible();
  await expect(async () => {
    await page.clock.runFor(5_000);
    await expect(w.text(payload("staff reply"))).toBeVisible({ timeout: 500 });
  }).toPass({ timeout: 15_000 });
  await w.send("five");
  await expect(w.text(payload("urgent follow-up"))).toBeVisible();
  await expect(page.locator("#vp-guidance h2")).toHaveText(SAFETY.guidanceHeadingUrgent);

  const facts = await page.evaluate(() => ({
    pwned: (window as unknown as { __pwned?: unknown }).__pwned ?? null,
    elements: document.querySelectorAll("img, svg, iframe, object, embed, a[href], form[action]").length,
    scripts: [...document.scripts].filter((script) => script.textContent?.includes("__pwned")).length,
    handlers: [...document.querySelectorAll("*")].filter((element) =>
      element.getAttributeNames().some((name) => name.startsWith("on")),
    ).length,
  }));
  expect(facts, "nothing ran and nothing was created").toEqual({ pwned: null, elements: 0, scripts: 0, handlers: 0 });
  expect(dialogs).toEqual([]);
  await expectStateChecks(page, { label: "XSS payloads", guidance: w.config.guidance, urgent: true });
});

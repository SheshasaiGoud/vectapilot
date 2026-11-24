/**
 * The confirmation card in a real browser, one test per row of leo's table (M8 PR 4; ADR 0016
 * W2, W5, W6, W9; `src/widget/core/confirm.ts`): every response, keyboard only, in en, hi and te,
 * at 360×800 and 1280×800. The confirm token stays in memory: never in the DOM or in storage
 * (S18), and every retry sends the same one.
 */

import { SAFETY } from "../src/widget/i18n/safety.ts";
import { expectStateChecks, press } from "./support/checks.ts";
import { Gate, expect, fixture, test } from "./support/stack.ts";
import type { FixtureCase, Reply } from "./support/stack.ts";
import { LANGUAGES, Widget, messageReply, templatePattern } from "./support/widget.ts";

const MESSAGES = "POST /v1/widget/messages";
const END = "DELETE /v1/widget/session";
interface Proposal {
  turn: { action_id: string; summary: string; confirm_token: string };
}
const PROPOSAL = (messageReply("confirmation-book").body as Proposal).turn;
const CONFIRM = `POST /v1/widget/actions/${PROPOSAL.action_id}/confirm`;
const message = (name: string) => (fixture("confirm", name).body as { message: string }).message;

for (const language of LANGUAGES) {
  test.describe(`confirmation card, ${language.lang} at ${language.business}`, () => {
    /** Opens the widget, gets the card, stubs confirm with `replies`. */
    async function card(page: Parameters<typeof press>[0], stack: Widget["stack"], replies: Reply[]) {
      const w = new Widget(page, stack, language);
      stack.on(MESSAGES, messageReply("confirmation-book"));
      if (replies.length > 0) stack.on(CONFIRM, ...replies);
      await w.open();
      await w.send();
      const box = page.locator(`#vp-cardbox-${PROPOSAL.action_id}`);
      await expect(box.getByText(PROPOSAL.summary, { exact: true })).toBeVisible();
      return {
        w,
        box,
        confirm: box.getByRole("button", { name: SAFETY.cardConfirm, exact: true }),
        tryAgain: box.getByRole("button", { name: SAFETY.cardTryAgain, exact: true }),
        notNow: box.getByRole("button", { name: SAFETY.cardNotNow, exact: true }),
      };
    }

    function tokensSent(stack: Widget["stack"]): unknown[] {
      return stack.callsTo(CONFIRM).map((call) => call.body);
    }
    const sameToken = (times: number) =>
      Array.from({ length: times }, () => ({ confirm_token: PROPOSAL.confirm_token }));

    test("the card: English summary, expiry, only the button confirms; the token stays in memory", async ({ page, stack }) => {
      const { w, box, confirm, notNow } = await card(page, stack, []);
      await expect(box.getByText(PROPOSAL.summary, { exact: true })).toHaveAttribute("lang", "en");
      await expect(box.getByText(templatePattern(SAFETY.cardExpires))).toBeVisible();
      await expect(box.getByText(SAFETY.cardOnlyButton, { exact: true })).toHaveAttribute("lang", "en");
      await expect(box.getByText(w.strings.inEnglish, { exact: true })).toBeVisible();
      await expect(confirm).toBeEnabled();
      await expect(notNow).toBeEnabled();
      expect(await page.content(), "S18: no confirm token in the DOM").not.toContain(PROPOSAL.confirm_token);
      const stored = await page.evaluate(() => JSON.stringify({ ...window.sessionStorage }));
      expect(stored).not.toContain(PROPOSAL.confirm_token);
      await expectStateChecks(page, { label: "confirmation card", guidance: null });
    });

    test("200: the executed message; focus stays on the card", async ({ page, stack }) => {
      const { box, confirm } = await card(page, stack, [fixture("confirm", "executed")]);
      await press(page, confirm);
      await expect(box.getByText(message("executed"), { exact: true })).toBeVisible();
      await expect(box.getByRole("button")).toHaveCount(0);
      await expect(box, "focus does not fall to the page (2.4.3)").toBeFocused();
      expect(tokensSent(stack)).toEqual(sameToken(1));
      const [call] = stack.callsTo(CONFIRM);
      expect(call.headers.authorization).toMatch(/^Bearer /);
      await expectStateChecks(page, { label: "confirm executed", guidance: null });
    });

    test("409: the fixed failure sentence", async ({ page, stack }) => {
      const { box, confirm } = await card(page, stack, [fixture("confirm", "failed")]);
      await press(page, confirm);
      await expect(box.getByText(message("failed"), { exact: true })).toBeVisible();
      await expect(box.getByRole("button")).toHaveCount(0);
      await expectStateChecks(page, { label: "confirm failed", guidance: null });
    });

    test("404: expired, nothing was done", async ({ page, stack }) => {
      const { box, confirm } = await card(page, stack, [fixture("confirm", "not-found")]);
      await press(page, confirm);
      await expect(box.getByText(SAFETY.cardExpired, { exact: true })).toBeVisible();
      await expectStateChecks(page, { label: "confirm expired", guidance: null });
    });

    for (const name of ["confirm-failed", "limits-unavailable"]) {
      test(`503 ${name}: not done yet, retried with the same token`, async ({ page, stack }) => {
        const { w, box, confirm, tryAgain } = await card(page, stack, [
          fixture("confirm", name),
          fixture("confirm", "executed"),
        ]);
        await press(page, confirm);
        await expect(box.getByText(SAFETY.cardNotDone, { exact: true })).toBeVisible();
        await expect(w.contact, "S16: the contact, as in any other failure").toBeVisible();
        await expectStateChecks(page, { label: `confirm ${name}`, guidance: w.guidance });
        await page.clock.runFor(10_000);
        expect(stack.count(CONFIRM), "no automatic retry when nothing was done").toBe(1);
        await press(page, tryAgain);
        await expect(box.getByText(message("executed"), { exact: true })).toBeVisible();
        expect(tokensSent(stack)).toEqual(sameToken(2));
      });
    }

    const unknowns: [string, () => Reply][] = [
      ["503 outcome_unknown", () => fixture("confirm", "outcome-unknown")],
      ["a network error", () => "network"],
    ];
    for (const [label, reply] of unknowns) {
      test(`${label}: we are checking, one automatic retry, then by hand`, async ({ page, stack }) => {
        const { w, box, confirm, tryAgain } = await card(page, stack, [
          reply(),
          reply(),
          fixture("confirm", "executed"),
        ]);
        await press(page, confirm);
        await expect(box.getByText(SAFETY.cardChecking, { exact: true })).toBeVisible();
        await expect(box.getByText(SAFETY.cardNotDone, { exact: true })).toHaveCount(0);
        await expectStateChecks(page, { label: `confirm ${label}, checking`, guidance: w.guidance });
        await page.clock.runFor(2_500); // the one automatic retry ([proposal]: after 2 s)
        await expect(box.getByText(SAFETY.cardStillChecking, { exact: true })).toBeVisible();
        await page.clock.runFor(30_000);
        expect(stack.count(CONFIRM), "exactly one automatic retry").toBe(2);
        await press(page, tryAgain);
        await expect(box.getByText(message("executed"), { exact: true })).toBeVisible();
        expect(tokensSent(stack)).toEqual(sameToken(3));
        await expect(page.locator("#vp-guidance"), "a definite answer clears the guidance").toHaveCount(0);
      });
    }

    test("503 outcome_unknown, then confirm_failed: still 'could not check', never 'not done' (leo's review)", async ({ page, stack }) => {
      const { w, box, confirm, tryAgain } = await card(page, stack, [
        fixture("confirm", "outcome-unknown"),
        fixture("confirm", "confirm-failed"),
        fixture("confirm", "executed"),
      ]);
      await press(page, confirm);
      await expect(box.getByText(SAFETY.cardChecking, { exact: true })).toBeVisible();
      await page.clock.runFor(2_500); // the automatic retry: this attempt changed nothing
      await expect(box.getByText(SAFETY.cardStillChecking, { exact: true })).toBeVisible();
      await expect(box.getByText(/nothing (was|happened)|not done/i)).toHaveCount(0);
      await expect(tryAgain).toBeEnabled();
      await expectStateChecks(page, { label: "confirm unknown, then confirm_failed", guidance: w.guidance });
      await press(page, tryAgain);
      await expect(box.getByText(message("executed"), { exact: true })).toBeVisible();
      expect(tokensSent(stack)).toEqual(sameToken(3));
    });

    test("End chat waits for a confirm; after an unknown outcome the ended view says so (leo's review)", async ({ page, stack }) => {
      const gate = new Gate();
      const { w, box, confirm } = await card(page, stack, [gate, fixture("confirm", "outcome-unknown")]);
      stack.on(END, fixture("end-session", "revoked"));
      await press(page, confirm);
      await gate.arrived;
      await expect(w.endButton, "never while the confirm may be running").toBeDisabled();
      gate.release(fixture("confirm", "outcome-unknown"));
      await expect(box.getByText(SAFETY.cardChecking, { exact: true })).toBeVisible();
      await expect(w.endButton).toBeDisabled();
      await page.clock.runFor(2_500);
      await expect(box.getByText(SAFETY.cardStillChecking, { exact: true })).toBeVisible();
      await press(page, w.endButton);
      await expect(w.text(w.strings.ended)).toBeVisible();
      await expect(w.text(SAFETY.cardUnknownHelp)).toHaveAttribute("lang", "en");
      await expect(page.getByText(/nothing (was|happened)/i)).toHaveCount(0);
      expect(stack.count(END)).toBe(1);
      await expectStateChecks(page, { label: "ended with an unknown outcome", guidance: null });
    });

    test("typing 'yes' is a new message: nothing is confirmed (S13, leo's review)", async ({ page, stack }) => {
      const { w, confirm } = await card(page, stack, []);
      stack.set(MESSAGES, messageReply("reply"));
      await w.send("yes");
      await expect(w.text(SAFETY.replyProvenance)).toBeVisible();
      expect(stack.callsTo(MESSAGES).map((call) => call.body)).toEqual([{ message: w.message }, { message: "yes" }]);
      expect(stack.count(CONFIRM)).toBe(0);
      await expect(confirm).toBeEnabled();
    });

    test("client timeout: we are checking, never 'nothing happened'", async ({ page, stack }) => {
      const never = new Gate();
      const { box, confirm } = await card(page, stack, [never, fixture("confirm", "executed")]);
      await press(page, confirm);
      await never.arrived;
      await expect(box.getByText(SAFETY.cardConfirming, { exact: true })).toBeVisible();
      await page.clock.runFor(16_000); // P7: the session's 15 s confirm bound
      await expect(box.getByText(SAFETY.cardChecking, { exact: true })).toBeVisible();
      await page.clock.runFor(2_500);
      await expect(box.getByText(message("executed"), { exact: true })).toBeVisible();
      expect(tokensSent(stack)).toEqual(sameToken(2));
    });

    test("429: a countdown, then the same token", async ({ page, stack }) => {
      const limited: FixtureCase = fixture("confirm", "rate-limited");
      const { w, box, confirm, tryAgain } = await card(page, stack, [limited, fixture("confirm", "executed")]);
      await press(page, confirm);
      await expect(box.getByText(SAFETY.cardRateLimited, { exact: true })).toBeVisible();
      // Shown, never announced: it sits in the log and changes every second (leo's review).
      const seconds = box.getByText(templatePattern(SAFETY.cardRetryIn));
      await expect(seconds).toBeVisible();
      await expect(seconds).toHaveAttribute("aria-hidden", "true");
      await expect(tryAgain).toBeDisabled();
      await expectStateChecks(page, { label: "confirm rate limited", guidance: w.guidance });
      await page.clock.runFor(61_000);
      await expect(box.getByText(SAFETY.cardCanTryAgain, { exact: true })).toBeVisible();
      await expect(seconds).toHaveCount(0);
      await press(page, tryAgain);
      await expect(box.getByText(message("executed"), { exact: true })).toBeVisible();
      expect(tokensSent(stack)).toEqual(sameToken(2));
    });

    test("401: the session ended; guidance shows", async ({ page, stack }) => {
      const { w, box, confirm } = await card(page, stack, [fixture("confirm", "unauthorized")]);
      await press(page, confirm);
      await expect(box.getByText(SAFETY.cardSessionEnded, { exact: true })).toBeVisible();
      await expect(box.getByText(SAFETY.cardUnknownHelp, { exact: true })).toHaveCount(0);
      await expect(w.status).toContainText(w.strings.sessionExpired);
      expect(await w.storageKeys()).toEqual([]);
      await expectStateChecks(page, { label: "confirm unauthorized", guidance: w.guidance });
    });

    test("401 after an unknown outcome: never claims nothing was done", async ({ page, stack }) => {
      const { w, box, confirm } = await card(page, stack, [
        fixture("confirm", "outcome-unknown"),
        fixture("confirm", "unauthorized"),
      ]);
      await press(page, confirm);
      await expect(box.getByText(SAFETY.cardChecking, { exact: true })).toBeVisible();
      await page.clock.runFor(2_500);
      await expect(box.getByText(SAFETY.cardSessionEnded, { exact: true })).toBeVisible();
      await expect(box.getByText(SAFETY.cardUnknownHelp, { exact: true })).toBeVisible();
      await expect(box.getByText(SAFETY.cardDismissed, { exact: true })).toHaveCount(0);
      await expectStateChecks(page, { label: "confirm 401 after unknown", guidance: w.guidance });
    });

    test("Not now: nothing was done, nothing sent", async ({ page, stack }) => {
      const { box, notNow } = await card(page, stack, []);
      await press(page, notNow);
      await expect(box.getByText(SAFETY.cardDismissed, { exact: true })).toBeVisible();
      await expect(box, "focus does not fall to the page (2.4.3)").toBeFocused();
      expect(stack.count(CONFIRM)).toBe(0);
      await expectStateChecks(page, { label: "not now", guidance: null });
    });

    test("an urgent hand-off drops the open card (D11)", async ({ page, stack }) => {
      const { w, box } = await card(page, stack, []);
      stack.set(MESSAGES, messageReply("handoff-urgent-guard-error"));
      await w.send(w.texts.quick_replies[1] ?? w.message);
      await expect(page.locator("#vp-guidance")).toBeVisible();
      await expect(box.getByText(SAFETY.cardDropped, { exact: true })).toBeVisible();
      await expect(box.getByRole("button")).toHaveCount(0);
      expect(stack.count(CONFIRM)).toBe(0);
      await expectStateChecks(page, { label: "card dropped", guidance: w.guidance, urgent: true });
    });
  });
}

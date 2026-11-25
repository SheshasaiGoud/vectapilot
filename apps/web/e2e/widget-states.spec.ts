/**
 * Every state of the customer widget in a real browser (M8 PR 4, step 4b; D8 (a)): each test runs
 * in en, hi and te (the last row, a rule without text of its own, in en only), and in both projects (360×800 and 1280×800, reduced motion). Keyboard only;
 * each state ends with `expectStateChecks` (guidance, reflow, axe, visible focus at every Tab
 * stop). The API is stubbed in the browser (`support/stack.ts`): $0, nothing leaves the machine.
 */

import { format } from "../src/widget/i18n/index.ts";
import { SAFETY } from "../src/widget/i18n/safety.ts";
import { expectStateChecks, press, tabTo } from "./support/checks.ts";
import { UI_ORIGIN } from "./support/serve.ts";
import { Gate, adaptBody, expect, fixture, test } from "./support/stack.ts";
import { LANGUAGES, Widget, followUpConfig, messageReply, templatePattern } from "./support/widget.ts";

const HANDOFF_ID = "6f1c2a9e-0d4b-4c8e-9a51-2b7e3f4a5c61";
const POLL = `GET /v1/widget/handoffs/${HANDOFF_ID}`;
const MESSAGES = "POST /v1/widget/messages";
const SESSIONS = "POST /v1/widget/sessions";
const HANDOFFS = "POST /v1/widget/handoffs";
const END = "DELETE /v1/widget/session";

interface AnswerBody {
  turn: { answer: { answer: string; sources: { title: string; content: string }[] } };
}
const ANSWER = (messageReply("answer").body as AnswerBody).turn.answer;
const REPLY_TEXT = (messageReply("reply").body as { turn: { text: string } }).turn.text;
const STAFF_REPLY = (fixture("handoff-status", "replied").body as { reply: string }).reply;

function tokenFor(business: string, lang: string): string {
  const created = adaptBody(fixture("sessions", "created").body, { businessId: business, lang, guidance: [] });
  return (created as { token: string }).token;
}

/** `count` code points of `unit`, repeated (S16's unit: what Python's `len()` counts). */
function codePoints(unit: string, count: number): string {
  return Array.from(unit.repeat(Math.ceil(count / Array.from(unit).length) + 1))
    .slice(0, count)
    .join("");
}

for (const language of LANGUAGES) {
  test.describe(`widget, ${language.lang} at ${language.business}`, () => {
    // ── Loading ─────────────────────────────────────────────────────────────────────────────────

    test("greeting: AI disclosure, Talk to a person, no session on page load", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      await w.open();
      await w.ready();
      await expect(page.locator("html")).toHaveAttribute("lang", language.lang);
      await expect(w.text(w.texts.greeting)).toBeVisible();
      await expect(w.text(w.texts.greeting)).toHaveAttribute("lang", language.lang);
      await expect(w.text(SAFETY.aiDisclosure)).toHaveAttribute("lang", "en");
      await expect(w.text(SAFETY.notForEmergencies)).toHaveAttribute("lang", "en");
      await expect(w.talkButton).toHaveAttribute("lang", "en");
      if (language.lang !== "en") {
        await expect(w.text(w.strings.safetyInEnglish)).toBeVisible();
        await expect(w.text(w.strings.translationUnreviewed)).toBeVisible();
      }
      for (const reply of w.texts.quick_replies) {
        await expect(page.getByRole("button", { name: reply, exact: true })).toBeVisible();
      }
      expect(stack.count(SESSIONS), "S14: no session on page load").toBe(0);
      expect(await w.storageKeys()).toEqual([]);
      await expectStateChecks(page, { label: "greeting", guidance: null });
    });

    test("configuration unreadable: its guidance still shows, then Try again (zara's review)", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      const broken = structuredClone(w.config) as unknown as { texts: { quick_replies: unknown[] }[] };
      broken.texts[0].quick_replies = [1]; // drift in one field: the rest fails closed
      const config = { status: 200, model: "WidgetConfigResponse", body: w.config };
      await w.open({ config: [{ status: 200, model: "none", body: broken }, config] });
      await expect(w.text(w.strings.unavailable)).toBeVisible();
      await expectStateChecks(page, { label: "unreadable configuration", guidance: w.guidance });
      await press(page, page.getByRole("button", { name: w.strings.retryLoad, exact: true }));
      await expect(w.text(w.texts.greeting)).toBeVisible();
      await expect(page.locator("#vp-guidance")).toHaveCount(0);
    });

    test("configuration unavailable, then Try again", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      const config = { status: 200, model: "WidgetConfigResponse", body: w.config };
      await w.open({ config: [fixture("config", "not-found"), config] });
      await expect(w.text(w.strings.unavailable)).toBeVisible();
      await expect(w.text(w.strings.unavailableHelp)).toBeVisible();
      await expectStateChecks(page, { label: "unavailable", guidance: null });
      await press(page, page.getByRole("button", { name: w.strings.retryLoad, exact: true }));
      await expect(w.text(w.texts.greeting)).toBeVisible();
    });

    // ── Sending ─────────────────────────────────────────────────────────────────────────────────

    test("working: one honest status, still under reduced motion", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      const gate = new Gate();
      stack.on(MESSAGES, gate);
      await w.open();
      await w.send();
      await gate.arrived;
      await expect(w.status).toContainText(w.strings.working);
      await expect(w.sendButton).toBeDisabled();
      expect(await page.evaluate(() => document.getAnimations().length), "no animation").toBe(0);
      await expectStateChecks(page, { label: "working", guidance: null });
      gate.release(messageReply("answer"));
      await expect(w.text(ANSWER.answer)).toBeVisible();
      await expect(w.status).toHaveText("");
    });

    test("quick reply: sent as the customer's own message, focus to the message box", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      stack.on(MESSAGES, messageReply("answer"));
      await w.open();
      const reply = w.texts.quick_replies[1] ?? w.message;
      await press(page, page.getByRole("button", { name: reply, exact: true }));
      await expect(w.text(ANSWER.answer)).toBeVisible();
      expect(stack.callsTo(MESSAGES).map((call) => call.body)).toEqual([{ message: reply }]);
      await expect(w.box, "focus does not fall to the page (2.4.3)").toBeFocused();
    });

    test("answer with citation chips, opened by keyboard; the requests' shape", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      stack.on(MESSAGES, messageReply("answer"));
      await w.open();
      await w.send();
      await expect(w.text(ANSWER.answer)).toBeVisible();
      await expect(w.text(w.strings.sources)).toBeVisible();
      const chip = page.locator("summary").first();
      await expect(chip).toContainText(ANSWER.sources[0].title);
      await press(page, chip);
      await expect(w.text(ANSWER.sources[0].content)).toBeVisible();

      const [session] = stack.callsTo(SESSIONS);
      expect(session.body).toEqual({ business_id: language.business, ui_language: language.lang });
      expect(session.headers.origin, "the page's origin, under no-referrer").toBe(UI_ORIGIN);
      expect(session.headers.referer).toBeUndefined();
      expect(session.headers.cookie).toBeUndefined();
      const [message] = stack.callsTo(MESSAGES);
      expect(message.headers.authorization).toBe(`Bearer ${tokenFor(language.business, language.lang)}`);
      expect(message.body).toEqual({ message: w.message });
      expect(await w.storageKeys(), "S18: the session token only").toEqual([
        `vectapilot.widget.session.${language.business}`,
      ]);
      await expectStateChecks(page, { label: "answer", guidance: null });
    });

    for (const name of ["answer-dont-know", "answer-no-sources"]) {
      test(`no answer (${name}): guidance, then the contact fallback`, async ({ page, stack }) => {
        const w = new Widget(page, stack, language);
        stack.on(MESSAGES, messageReply(name));
        await w.open();
        await w.send();
        const noAnswer = format(w.strings.noAnswer, { name: w.config.display_name });
        await expect(w.status).toContainText(noAnswer);
        await expect(w.contact).toBeVisible();
        await expect(w.text(w.strings.sources)).toHaveCount(0); // S9: never an answer without sources
        await expectStateChecks(page, { label: name, guidance: w.guidance });
      });
    }

    test("reply with its provenance label", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      stack.on(MESSAGES, messageReply("reply"));
      await w.open();
      await w.send();
      await expect(w.text(REPLY_TEXT)).toBeVisible();
      await expect(w.text(SAFETY.replyProvenance)).toHaveAttribute("lang", "en");
      await expectStateChecks(page, { label: "reply", guidance: null });
    });

    test("over length: 2,001 code points cannot be sent", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      await w.open();
      await tabTo(page, w.box);
      await page.keyboard.insertText(codePoints(w.message, 2001));
      await expect(page.locator("#vp-over")).toHaveText(format(w.strings.overLimit, { over: 1 }));
      await expect(page.locator("#vp-counter")).toHaveText(
        format(w.strings.counter, { count: 2001, max: 2000 }),
      );
      await expect(w.box).toHaveAttribute("aria-invalid", "true");
      await expect(w.sendButton).toBeDisabled();
      await page.keyboard.press("Enter");
      expect(stack.count(SESSIONS) + stack.count(MESSAGES), "nothing sent").toBe(0);
      await expectStateChecks(page, { label: "over length", guidance: null });
      await tabTo(page, w.box);
      await page.keyboard.press("Backspace");
      await expect(page.locator("#vp-over")).toHaveCount(0);
    });

    // ── Hand-offs ───────────────────────────────────────────────────────────────────────────────

    test("urgent hand-off: guidance first, never a reply prompt, no polling", async ({ page, stack }) => {
      const w = new Widget(page, stack, language, followUpConfig(language.business));
      stack.on(MESSAGES, messageReply("handoff-urgent-guard-error"));
      await w.open();
      await w.send();
      await expect(page.locator("#vp-guidance")).toBeVisible();
      if (w.texts.urgent_follow_up) await expect(w.text(w.texts.urgent_follow_up)).toBeVisible();
      if (w.texts.handoff_follow_up) await expect(w.text(w.texts.handoff_follow_up)).toHaveCount(0);
      // S8 as decided: the contact fallback too; neither a reply prompt nor an ETA (zara's review).
      await expect(w.contact).toBeVisible();
      await page.clock.runFor(30_000);
      expect(stack.count(/^GET \/v1\/widget\/handoffs\//), "S8: no polling").toBe(0);
      await expectStateChecks(page, { label: "urgent hand-off", guidance: w.guidance, urgent: true });
    });

    test("Talk to a person while a message is in flight: no follow-up until that message answers", async ({ page, stack }) => {
      const w = new Widget(page, stack, language, followUpConfig(language.business));
      const gate = new Gate();
      stack.on(MESSAGES, gate);
      stack.on(HANDOFFS, fixture("handoffs", "created"));
      stack.on(POLL, fixture("handoff-status", "open"));
      await w.open();
      await w.send();
      await gate.arrived;
      await press(page, w.talkButton);
      await expect(w.contact).toBeVisible();
      if (w.texts.handoff_follow_up) await expect(w.text(w.texts.handoff_follow_up)).toHaveCount(0);
      await page.clock.runFor(10_000);
      expect(stack.count(POLL), "S8: no polling while the row may still turn urgent").toBe(0);
      gate.release(messageReply("handoff-urgent-guard-error")); // S17: it raised the same row
      await expect(page.locator("#vp-guidance h2")).toHaveText(SAFETY.guidanceHeadingUrgent);
      if (w.texts.urgent_follow_up) await expect(w.text(w.texts.urgent_follow_up)).toBeVisible();
      if (w.texts.handoff_follow_up) await expect(w.text(w.texts.handoff_follow_up)).toHaveCount(0);
      await page.clock.runFor(10_000);
      expect(stack.count(POLL)).toBe(0);
      await expectStateChecks(page, { label: "talk to a person in flight", guidance: w.guidance, urgent: true });
    });

    test("safety hand-off not stored: guidance and the contact fallback", async ({ page, stack }) => {
      const w = new Widget(page, stack, language, followUpConfig(language.business));
      stack.on(MESSAGES, messageReply("handoff-safety-not-stored"));
      await w.open();
      await w.send();
      await expect(w.contact).toBeVisible();
      for (const text of [w.texts.urgent_follow_up, w.texts.handoff_follow_up]) {
        if (text) await expect(w.text(text)).toHaveCount(0);
      }
      await expectStateChecks(page, { label: "safety hand-off, not stored", guidance: w.guidance, urgent: true });
    });

    test("normal hand-off stored: the follow-up, polling, then the staff reply", async ({ page, stack }) => {
      const w = new Widget(page, stack, language, followUpConfig(language.business));
      stack.on(MESSAGES, messageReply("handoff-normal-queued"));
      stack.on(POLL, fixture("handoff-status", "open"), fixture("handoff-status", "replied"));
      await w.open();
      await w.send();
      await expect(w.contact).toBeVisible();
      if (w.texts.handoff_follow_up) await expect(w.text(w.texts.handoff_follow_up)).toBeVisible();
      await expectStateChecks(page, { label: "normal hand-off", guidance: w.guidance });
      // One 5 s poll at a time (P7) until the reply is on screen: "open", then "replied".
      await expect(async () => {
        await page.clock.runFor(5_000);
        await expect(w.text(STAFF_REPLY)).toBeVisible({ timeout: 500 });
      }).toPass({ timeout: 15_000 });
      expect(stack.count(POLL)).toBeGreaterThanOrEqual(2);
      await expect(
        w.text(format(w.strings.staffReply, { name: w.config.display_name })),
      ).toBeVisible();
      const polls = stack.count(POLL);
      await page.clock.runFor(10_000);
      expect(stack.count(POLL), "polling stops at the reply").toBe(polls);
      await expectStateChecks(page, { label: "staff reply", guidance: w.guidance });
    });

    test("normal hand-off not stored: the contact fallback, no follow-up", async ({ page, stack }) => {
      const w = new Widget(page, stack, language, followUpConfig(language.business));
      stack.on(MESSAGES, messageReply("handoff-normal-not-queued"));
      await w.open();
      await w.send();
      await expect(w.contact).toBeVisible();
      if (w.texts.handoff_follow_up) await expect(w.text(w.texts.handoff_follow_up)).toHaveCount(0);
      await page.clock.runFor(10_000);
      expect(stack.count(/^GET \/v1\/widget\/handoffs\//)).toBe(0);
      await expectStateChecks(page, { label: "normal hand-off, not stored", guidance: w.guidance });
    });

    test("Talk to a person, stored: the follow-up", async ({ page, stack }) => {
      const w = new Widget(page, stack, language, followUpConfig(language.business));
      stack.on(HANDOFFS, fixture("handoffs", "created"));
      stack.on(POLL, fixture("handoff-status", "open"));
      await w.open();
      await w.ready();
      await press(page, w.talkButton);
      await expect(w.contact).toBeVisible();
      if (w.texts.handoff_follow_up) await expect(w.text(w.texts.handoff_follow_up)).toBeVisible();
      expect(stack.callsTo(HANDOFFS).map((call) => call.body)).toEqual([{}]);
      expect(stack.count(SESSIONS), "S14: created lazily, on this press").toBe(1);
      await expectStateChecks(page, { label: "talk to a person", guidance: w.guidance });
    });

    // D11 decided by the server (zara's PR 2 review): a queued row with `follow_up` false is
    // urgent or `safety` as stored. No poll is stubbed: one would reach the catch-all.
    test("Talk to a person, the row urgent (follow_up false): D11's text, no generic follow-up, no polling", async ({ page, stack }) => {
      const w = new Widget(page, stack, language, followUpConfig(language.business));
      stack.on(HANDOFFS, fixture("handoffs", "created-urgent-row"));
      await w.open();
      await w.ready();
      await press(page, w.talkButton);
      await expect(w.contact).toBeVisible();
      await expect(page.locator("#vp-guidance h2")).toHaveText(SAFETY.guidanceHeadingUrgent);
      if (w.texts.urgent_follow_up) await expect(w.text(w.texts.urgent_follow_up)).toBeVisible();
      if (w.texts.handoff_follow_up) await expect(w.text(w.texts.handoff_follow_up)).toHaveCount(0);
      await page.clock.runFor(30_000);
      expect(stack.count(/^GET \/v1\/widget\/handoffs\//), "S8: no polling").toBe(0);
      await expectStateChecks(page, { label: "talk to a person, urgent row", guidance: w.guidance, urgent: true });
    });

    test("normal hand-off stored, then raised (status follow_up false): polling stops, the follow-up goes", async ({ page, stack }) => {
      const w = new Widget(page, stack, language, followUpConfig(language.business));
      const followUp = w.texts.handoff_follow_up ?? "";
      expect(followUp, "a follow-up to withdraw: the check is never vacuous").not.toBe("");
      stack.on(MESSAGES, messageReply("handoff-normal-queued"));
      stack.on(POLL, fixture("handoff-status", "open"), fixture("handoff-status", "raised"));
      await w.open();
      await w.send();
      await expect(w.text(followUp)).toBeVisible();
      await expect(async () => {
        await page.clock.runFor(5_000);
        await expect(w.text(followUp)).toHaveCount(0, { timeout: 500 });
      }).toPass({ timeout: 15_000 });
      expect(stack.count(POLL)).toBe(2);
      await expect(w.contact).toBeVisible();
      // zara's PR 4 review: the message's and the press's whole rule, the urgent heading and
      // D11 (b)'s text included.
      await expect(page.locator("#vp-guidance h2")).toHaveText(SAFETY.guidanceHeadingUrgent);
      if (w.texts.urgent_follow_up) await expect(w.text(w.texts.urgent_follow_up)).toBeVisible();
      await page.clock.runFor(30_000);
      expect(stack.count(POLL), "polling stopped").toBe(2);
      await expectStateChecks(page, { label: "hand-off raised", guidance: w.guidance, urgent: true });
    });

    // zara's PR 4 review: a 404 at the status read means no reply can reach this view.
    for (const [name, ended] of [
      ["not-found", false],
      ["widget-not-found", true],
    ] as const) {
      test(`normal hand-off stored, then the status read answers 404 ${name}: polling stops, the follow-up goes`, async ({ page, stack }) => {
        const w = new Widget(page, stack, language, followUpConfig(language.business));
        const followUp = w.texts.handoff_follow_up ?? "";
        expect(followUp, "a follow-up to withdraw: the check is never vacuous").not.toBe("");
        stack.on(MESSAGES, messageReply("handoff-normal-queued"));
        stack.on(POLL, fixture("handoff-status", name));
        await w.open();
        await w.send();
        await expect(w.text(followUp)).toBeVisible();
        await expect(async () => {
          await page.clock.runFor(5_000);
          await expect(w.text(followUp)).toHaveCount(0, { timeout: 500 });
        }).toPass({ timeout: 15_000 });
        expect(stack.count(POLL)).toBe(1);
        await expect(w.text(w.strings.sessionExpired)).toHaveCount(ended ? 1 : 0);
        await page.clock.runFor(30_000);
        expect(stack.count(POLL), "polling stopped").toBe(1);
        await expectStateChecks(page, { label: `status read 404 ${name}`, guidance: w.guidance });
      });
    }

    test("Talk to a person, not stored: the contact fallback, no follow-up", async ({ page, stack }) => {
      const w = new Widget(page, stack, language, followUpConfig(language.business));
      stack.on(HANDOFFS, fixture("handoffs", "not-stored"));
      await w.open();
      await w.ready();
      await press(page, w.talkButton);
      await expect(w.contact).toBeVisible();
      if (w.texts.handoff_follow_up) await expect(w.text(w.texts.handoff_follow_up)).toHaveCount(0);
      await expectStateChecks(page, { label: "talk to a person, not stored", guidance: w.guidance });
    });

    // Z4 (b), decided 2026-11-24 (shesha): a hand-off that carries guidance was not cleared by a
    // safety step, so no follow-up and no polling for the rest of the session. No poll is
    // stubbed: one would reach the catch-all and fail the test (the stack stays closed).
    for (const name of ["handoff-not-covered", "handoff-safety-floor-off"]) {
      test(`${name}: carries guidance, not cleared (Z4 (b)): no follow-up, no polling, not after Talk to a person either`, async ({ page, stack }) => {
        const w = new Widget(page, stack, language, followUpConfig(language.business));
        const followUp = w.texts.handoff_follow_up ?? "";
        expect(followUp, "a follow-up to withhold: the check is never vacuous").not.toBe("");
        const button = new Gate();
        stack.on(MESSAGES, messageReply(name));
        stack.on(HANDOFFS, button);
        await w.open();
        await w.send();
        await expect(page.locator("#vp-guidance")).toBeVisible();
        await expect(w.contact).toBeVisible();
        if (name === "handoff-not-covered") await expect(w.text(w.strings.notCoveredLanguage)).toBeVisible();
        await expect(w.text(followUp)).toHaveCount(0);
        await page.clock.runFor(30_000);
        expect(stack.count(/^GET \/v1\/widget\/handoffs\//), "Z4 (b): no polling").toBe(0);
        await expectStateChecks(page, { label: `${name}, not cleared`, guidance: w.guidance });
        // Remembered for the session: the button's own customer_asked row gets no follow-up.
        await press(page, w.talkButton);
        await button.arrived;
        await expect(w.talkButton).toBeDisabled();
        button.release(fixture("handoffs", "created"));
        await expect(w.talkButton).toBeEnabled(); // its answer has landed
        await expect(w.contact).toBeVisible();
        await expect(w.text(followUp)).toHaveCount(0);
        await page.clock.runFor(30_000);
        expect(stack.count(/^GET \/v1\/widget\/handoffs\//), "Z4 (b): still no polling").toBe(0);
        expect(stack.count(SESSIONS), "the same session").toBe(1);
        await expectStateChecks(page, { label: `talk to a person after ${name}`, guidance: w.guidance });
      });
    }

    test("unknown kind: the safe hand-off state", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      stack.on(MESSAGES, messageReply("unknown-kind"));
      await w.open();
      await w.send();
      await expect(w.status).toContainText(w.strings.notAnswered);
      await expect(w.contact).toBeVisible();
      await expectStateChecks(page, { label: "unknown kind", guidance: w.guidance });
    });

    // ── Failures: guidance first, the typed message kept (S16) ──────────────────────────────────

    test("network error: guidance shows, the message is kept", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      stack.on(MESSAGES, "network");
      await w.open();
      await w.send();
      await expect(page.locator("#vp-guidance")).toBeVisible();
      await expect(w.status).toContainText(w.strings.networkError);
      await expect(w.status).toContainText(w.strings.mayStillBeProcessed);
      await expect(w.status).toContainText(w.strings.messageKept);
      await expect(w.box).toHaveValue(w.message);
      await expect(w.text(w.strings.mayBeProcessed)).toBeVisible();
      await expect(w.contact).toBeVisible();
      expect(stack.count(MESSAGES), "D15: never re-sent by itself").toBe(1);
      await expectStateChecks(page, { label: "network error", guidance: w.guidance });
    });

    test("client timeout from the session's bound: may still be processed", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      const never = new Gate(); // never released: no answer at all
      stack.on(MESSAGES, never);
      await w.open();
      await w.send();
      await never.arrived; // the clock moves only once the message is out (not the session call)
      await expect(w.status).toContainText(w.strings.working);
      // P7: the bound is the session response's 65 s, not the 15 s of the other calls. The clock
      // also flows with real time, so the check before the bound keeps a 10 s margin.
      await page.clock.runFor(55_000);
      await expect(w.status).toContainText(w.strings.working);
      await page.clock.runFor(11_000);
      await expect(w.status).toContainText(w.strings.timeout);
      await expect(w.status).toContainText(w.strings.mayStillBeProcessed);
      await expect(w.box).toHaveValue(w.message);
      await page.clock.runFor(120_000);
      expect(stack.count(MESSAGES), "D15: never re-sent by itself").toBe(1);
      await expectStateChecks(page, { label: "client timeout", guidance: w.guidance });
    });

    test("session expired (401): the token is forgotten, the message kept", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      stack.on(MESSAGES, messageReply("unauthorized"));
      await w.open();
      await w.send();
      await expect(w.status).toContainText(w.strings.sessionExpired);
      await expect(w.box).toHaveValue(w.message);
      expect(await w.storageKeys()).toEqual([]);
      await expectStateChecks(page, { label: "session expired", guidance: w.guidance });
    });

    test("invalid message (422)", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      stack.on(MESSAGES, messageReply("invalid"));
      await w.open();
      await w.send();
      await expect(w.status).toContainText(w.strings.invalidMessage);
      await expect(w.box).toHaveValue(w.message);
      await expect(w.text(w.strings.notSent)).toBeVisible();
      await expectStateChecks(page, { label: "invalid message", guidance: w.guidance });
    });

    test("rate limited (429): a countdown, then sending is possible again", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      stack.on(MESSAGES, messageReply("rate-limited"));
      await w.open();
      await w.send();
      await expect(w.status).toContainText(w.strings.rateLimited);
      await expect(page.getByText(templatePattern(w.strings.retryIn))).toBeVisible();
      // The server's limiter decides; a red-flag message skips it (M7 G1, zara's review).
      await expect(w.sendButton).toBeEnabled();
      await expectStateChecks(page, { label: "rate limited", guidance: w.guidance });
      await page.clock.runFor(13_000);
      await expect(w.status).toContainText(w.strings.canSendAgain);
      await expect(w.sendButton).toBeEnabled();
      expect(stack.count(MESSAGES), "D15: never re-sent by itself").toBe(1);
    });

    test("server error (500): may still be processed", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      stack.on(MESSAGES, messageReply("server-error"));
      await w.open();
      await w.send();
      await expect(w.status).toContainText(w.strings.serverError);
      await expect(w.status).toContainText(w.strings.mayStillBeProcessed);
      await expect(w.box).toHaveValue(w.message);
      await expect(w.contact).toBeVisible();
      await expectStateChecks(page, { label: "server error", guidance: w.guidance });
    });

    test("session lookup unavailable (typed 503): not sent", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      stack.on(MESSAGES, messageReply("store-unavailable"));
      await w.open();
      await w.send();
      await expect(w.status).toContainText(w.strings.serverError);
      await expect(w.status).not.toContainText(w.strings.mayStillBeProcessed);
      await expect(w.text(w.strings.notSent)).toBeVisible();
      await expectStateChecks(page, { label: "session lookup unavailable", guidance: w.guidance });
    });

    test("session creation refused (Redis down): guidance and contact, never a reply promise", async ({ page, stack }) => {
      const w = new Widget(page, stack, language, followUpConfig(language.business));
      await w.open({ session: [fixture("sessions", "limits-unavailable")] });
      await w.send();
      await expect(w.status).toContainText(w.strings.sessionUnavailable);
      await expect(w.contact).toBeVisible();
      if (w.texts.handoff_follow_up) await expect(w.text(w.texts.handoff_follow_up)).toHaveCount(0);
      await expect(w.box).toHaveValue(w.message);
      expect(stack.count(MESSAGES)).toBe(0);
      expect(await w.storageKeys()).toEqual([]);
      await expectStateChecks(page, { label: "session creation refused", guidance: w.guidance });
    });

    test("session creation rate limited (429): a countdown", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      await w.open({ session: [fixture("sessions", "rate-limited")] });
      await w.send();
      await expect(w.status).toContainText(w.strings.rateLimited);
      await expect(page.getByText(templatePattern(w.strings.retryIn))).toBeVisible();
      expect(stack.count(MESSAGES)).toBe(0);
      await expectStateChecks(page, { label: "session creation rate limited", guidance: w.guidance });
    });

    // ── Ending ──────────────────────────────────────────────────────────────────────────────────

    test("End chat: revoked, cleared, focus to Start a new chat", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      stack.on(MESSAGES, messageReply("answer"));
      stack.on(END, fixture("end-session", "revoked"));
      await w.open();
      await w.send();
      await expect(w.text(ANSWER.answer)).toBeVisible();
      await press(page, w.endButton);
      await expect(w.text(w.strings.ended)).toBeVisible();
      const startNew = page.getByRole("button", { name: w.strings.startNew, exact: true });
      await expect(startNew, "focus does not fall to the page (2.4.3)").toBeFocused();
      const [end] = stack.callsTo(END);
      expect(end.headers.authorization).toBe(`Bearer ${tokenFor(language.business, language.lang)}`);
      expect(await w.storageKeys()).toEqual([]);
      await expect(w.text(ANSWER.answer)).toHaveCount(0);
      await expectStateChecks(page, { label: "ended", guidance: null });
      await press(page, startNew);
      await expect(w.text(w.texts.greeting)).toBeVisible();
      await expect(w.box).toBeFocused();
    });

    test("End chat after an urgent hand-off: the guidance stays (zara's review)", async ({ page, stack }) => {
      const w = new Widget(page, stack, language);
      stack.on(MESSAGES, messageReply("handoff-urgent-guard-error"));
      stack.on(END, fixture("end-session", "revoked"));
      await w.open();
      await w.send();
      await expect(page.locator("#vp-guidance")).toBeVisible();
      await press(page, w.endButton);
      await expect(w.text(w.strings.ended)).toBeVisible();
      expect(await w.storageKeys()).toEqual([]);
      await expect(w.text(w.message)).toHaveCount(0); // the transcript is cleared
      await expectStateChecks(page, { label: "ended after an urgent hand-off", guidance: w.guidance, urgent: true });
    });
  });
}

// "Talk to a person" answered 404 is a 401 (decided 2026-11-25, shesha), as confirm and the status
// read: one language is enough (the rule has no text of its own), both projects. No poll is
// stubbed: one would reach the closed catch-all and fail the test.
test.describe("widget, en: Talk to a person answered 404", () => {
  const english = LANGUAGES.find((language) => language.lang === "en")!;

  test("404 widget_not_found: as a 401, guidance and contact, no follow-up, a new session on the next send", async ({ page, stack }) => {
    const w = new Widget(page, stack, english, followUpConfig(english.business));
    stack.on(HANDOFFS, fixture("handoffs", "widget-not-found"));
    stack.on(MESSAGES, messageReply("reply"));
    await w.open();
    await w.ready();
    await press(page, w.talkButton);
    await expect(w.status).toContainText(w.strings.sessionExpired);
    await expect(w.contact).toBeVisible();
    await expect(page.locator("#vp-guidance h2")).toHaveText(SAFETY.guidanceHeading);
    for (const text of [w.texts.handoff_follow_up, w.texts.urgent_follow_up]) {
      expect(text, "a follow-up to rule out: the check is never vacuous").toBeTruthy();
      await expect(w.text(text!)).toHaveCount(0);
    }
    expect(await w.storageKeys()).toEqual([]);
    await page.clock.runFor(30_000);
    expect(stack.count(/^GET \/v1\/widget\/handoffs\//), "no polling").toBe(0);
    await expectStateChecks(page, { label: "talk to a person, widget_not_found", guidance: w.guidance });
    await w.send();
    await expect(w.text(REPLY_TEXT)).toBeVisible();
    expect(stack.count(SESSIONS), "the next send opens a new session").toBe(2);
    expect(stack.count(HANDOFFS)).toBe(1);
  });
});

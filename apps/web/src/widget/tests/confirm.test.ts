/** The confirmation card, one test per row of leo's table (M8 PR 4; ADR 0016 W2, W5, W6, W9). */

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { SAFETY } from "../i18n/safety.ts";
import { CONFIRM_AUTO_RETRY_MS } from "../core/controller.ts";
import type { CardView } from "../core/view.ts";
import { FakeApi, ROUTE, fixture, openWidget, settle, shownTexts } from "./harness.ts";
import type { Harness, Reply } from "./harness.ts";

const ACTION = "3d6f0a52-8b1e-4f7a-9c2d-5e8b1a7f4c30";
const CONFIRM_TOKEN = "Fx7_fixture-token-0001";
const CONFIRM = ROUTE.confirm(ACTION);
const CONFIG = fixture("config", "sunrise-clinic").body as { guidance: string[]; texts: { contact_fallback: string[] }[] };
const GUIDANCE = CONFIG.guidance;
const CONTACT = CONFIG.texts[0].contact_fallback;
const EXECUTED = "Done: the confirmed action was carried out.";
/** Never after an unknown outcome (leo's review): no text may say or imply nothing was done. */
const NOTHING = /nothing (was|happened)|not done/i;

async function withCard(...replies: Reply[]): Promise<Harness> {
  const api = new FakeApi()
    .on(ROUTE.session, fixture("sessions", "created"))
    .on(ROUTE.message, fixture("messages", "confirmation-book"))
    .on(CONFIRM, ...replies);
  const h = await openWidget({ api });
  await h.say("Book me in on Tuesday");
  return h;
}

function card(h: Harness): CardView {
  const entry = h.view().transcript.find((item) => item.kind === "confirm");
  assert.ok(entry && entry.kind === "confirm");
  return entry.card;
}

async function press(h: Harness): Promise<void> {
  const pending = h.widget.confirm(ACTION);
  await settle();
  await pending;
}

function confirmCalls(h: Harness) {
  return h.api.calls.filter((call) => `${call.method} ${call.path}` === CONFIRM);
}

function assertSameToken(h: Harness): void {
  assert.ok(confirmCalls(h).length > 0);
  for (const call of confirmCalls(h)) assert.deepEqual(call.body, { confirm_token: CONFIRM_TOKEN });
}

function assertNeverNothing(h: Harness): void {
  const said = shownTexts(h.view()).filter((text) => NOTHING.test(text));
  assert.deepEqual(said, [], "never 'nothing was done' or 'not done' after an unknown outcome");
}

function assertGuidanceAndContact(h: Harness, guidance: string[] = GUIDANCE): void {
  const view = h.view();
  assert.equal(view.notice?.kind, "confirm_problem");
  assert.deepEqual(view.guidance?.lines.map((line) => line.text), guidance);
  assert.ok(view.guidance?.lines.every((line) => line.lang === "en"));
  assert.equal(view.guidance?.heading.text, SAFETY.guidanceHeading);
  for (const line of CONTACT) assert.ok(shownTexts(view).includes(line), line);
}

function assertEnglish(view: CardView): void {
  for (const line of [view.heading, view.summary, view.status, view.extra, view.onlyButton, view.expires]) {
    if (line !== null) assert.equal(line.lang, "en", line.text);
  }
}

describe("the card before any confirm", () => {
  test("English summary with a label, expiry as text, the only-the-button line, both buttons", async () => {
    const h = await withCard();
    const view = card(h);
    assertEnglish(view);
    assert.equal(view.summary.text, "Book General consultation on Tuesday 1 December 2026 at 10:30 (30 minutes).");
    assert.equal(view.englishLabel.text, "In English");
    assert.match(view.expires?.text ?? "", /^Expires at /);
    assert.equal(view.onlyButton?.text, SAFETY.cardOnlyButton);
    assert.deepEqual(view.confirm, { label: { text: "Confirm", lang: "en" }, enabled: true });
    assert.equal(view.notNow?.enabled, true);
  });

  test("both buttons are disabled while a confirm is in flight", async () => {
    const h = await withCard("hang");
    void h.widget.confirm(ACTION);
    await settle();
    const view = card(h);
    assert.equal(view.busy, true);
    assert.equal(view.status?.text, SAFETY.cardConfirming);
    assert.equal(view.confirm?.enabled, false);
    assert.equal(view.notNow?.enabled, false);
    await h.widget.confirm(ACTION);
    assert.equal(confirmCalls(h).length, 1, "a second press sends nothing");
  });

  test("the body is the token alone, sent with the session's bearer", async () => {
    const h = await withCard(fixture("confirm", "executed"));
    await press(h);
    const [call] = confirmCalls(h);
    assert.deepEqual(call.body, { confirm_token: CONFIRM_TOKEN });
    assert.match(call.headers.authorization, /^Bearer sunrise-clinic\./);
  });
});

describe("one test per response", () => {
  test("200: the fixed executed message; done", async () => {
    const h = await withCard(fixture("confirm", "executed"));
    await press(h);
    const view = card(h);
    assert.equal(view.status?.text, "Done: the confirmed action was carried out.");
    assert.equal(view.confirm, null);
    assert.equal(view.notNow, null);
    await h.widget.confirm(ACTION);
    assert.equal(confirmCalls(h).length, 1);
  });

  test("409: the fixed failure sentence; done", async () => {
    const h = await withCard(fixture("confirm", "failed"));
    await press(h);
    assert.equal(card(h).status?.text, "That slot is no longer free, so nothing was booked.");
    assert.equal(card(h).confirm, null);
  });

  test("404 action_not_found: 'Expired. Nothing was done.'; done", async () => {
    const h = await withCard(fixture("confirm", "not-found"));
    await press(h);
    assert.equal(card(h).status?.text, SAFETY.cardExpired);
    assert.equal(card(h).confirm, null);
  });

  for (const name of ["confirm-failed", "limits-unavailable"]) {
    test(`503 ${name}: 'Not done yet'; a manual retry with the same token`, async () => {
      const h = await withCard(fixture("confirm", name), fixture("confirm", "executed"));
      await press(h);
      assert.equal(card(h).status?.text, SAFETY.cardNotDone);
      assert.deepEqual(card(h).confirm, { label: { text: "Try again", lang: "en" }, enabled: true });
      await h.timers.advance(60_000);
      assert.equal(confirmCalls(h).length, 1, "no automatic retry");
      await press(h);
      const calls = confirmCalls(h);
      assert.equal(calls.length, 2);
      assert.deepEqual(calls[1].body, { confirm_token: CONFIRM_TOKEN });
      assert.equal(card(h).status?.text, "Done: the confirmed action was carried out.");
    });
  }

  for (const [label, reply] of [
    ["503 outcome_unknown", fixture("confirm", "outcome-unknown")],
    ["network error", "network"],
  ] as const) {
    test(`${label}: 'We are checking', one automatic retry, then manual; never 'nothing'`, async () => {
      const h = await withCard(reply, reply, fixture("confirm", "executed"));
      await press(h);
      assert.equal(card(h).status?.text, SAFETY.cardChecking);
      assert.equal(card(h).confirm?.enabled, false);
      await h.timers.advance(CONFIRM_AUTO_RETRY_MS);
      assert.equal(confirmCalls(h).length, 2, "one automatic retry");
      assert.equal(card(h).status?.text, SAFETY.cardStillChecking);
      await h.timers.advance(10 * 60_000);
      assert.equal(confirmCalls(h).length, 2, "only one automatic retry");
      assert.ok(confirmCalls(h).every((call) => (call.body as { confirm_token: string }).confirm_token === CONFIRM_TOKEN));
      assert.ok(!shownTexts(h.view()).some((text) => /nothing (was|happened)/i.test(text)));
      await press(h);
      assert.equal(card(h).status?.text, "Done: the confirmed action was carried out.");
    });
  }

  test("client timeout: 'We are checking' after the session's confirm timeout, then one retry", async () => {
    const h = await withCard("hang", fixture("confirm", "executed"));
    void h.widget.confirm(ACTION);
    await settle();
    await h.timers.advance(14_999);
    assert.equal(card(h).status?.text, SAFETY.cardConfirming, "the session said 15 s");
    await h.timers.advance(1);
    assert.equal(card(h).status?.text, SAFETY.cardChecking);
    await h.timers.advance(CONFIRM_AUTO_RETRY_MS);
    assert.equal(confirmCalls(h).length, 2);
    assert.equal(card(h).status?.text, "Done: the confirmed action was carried out.");
  });

  test("429: a countdown, then the same token", async () => {
    const h = await withCard(fixture("confirm", "rate-limited"), fixture("confirm", "executed"));
    await press(h);
    assert.equal(card(h).status?.text, SAFETY.cardRateLimited);
    assert.equal(card(h).countdown?.text, "Try again in 9 s.");
    assert.equal(card(h).confirm?.enabled, false);
    await h.timers.advance(4_000);
    assert.equal(card(h).status?.text, SAFETY.cardRateLimited, "the announced line does not change");
    assert.equal(card(h).countdown?.text, "Try again in 5 s.");
    await h.widget.confirm(ACTION);
    assert.equal(confirmCalls(h).length, 1, "not before the countdown ends");
    await h.timers.advance(5_000);
    assert.equal(card(h).status?.text, SAFETY.cardCanTryAgain);
    assert.equal(card(h).countdown, null);
    assert.equal(card(h).confirm?.enabled, true);
    await press(h);
    assert.deepEqual(confirmCalls(h)[1].body, { confirm_token: CONFIRM_TOKEN });
  });

  test("401: session ended; the guidance (S16); no claim either way", async () => {
    const h = await withCard(fixture("confirm", "unauthorized"));
    await press(h);
    assert.equal(card(h).status?.text, SAFETY.cardSessionEnded);
    assert.equal(card(h).extra, null);
    assert.equal(card(h).confirm, null);
    assert.equal(h.view().notice?.kind, "session_expired");
    assert.ok(h.view().guidance);
  });

  test("401 after an unknown outcome: it does not claim nothing was done", async () => {
    const h = await withCard(fixture("confirm", "outcome-unknown"), fixture("confirm", "unauthorized"));
    await press(h);
    await h.timers.advance(CONFIRM_AUTO_RETRY_MS);
    assert.equal(card(h).status?.text, SAFETY.cardSessionEnded);
    assert.equal(card(h).extra?.text, SAFETY.cardUnknownHelp);
    assert.ok(!shownTexts(h.view()).some((text) => /nothing was done/i.test(text)));
  });
});

describe("Not now", () => {
  test("drops the token: 'Not confirmed. Nothing was done.'; never sent later", async () => {
    const h = await withCard();
    h.widget.notNow(ACTION);
    assert.equal(card(h).status?.text, SAFETY.cardDismissed);
    await h.widget.confirm(ACTION);
    assert.equal(confirmCalls(h).length, 0);
  });

  test("after an unknown outcome it says it could not check, never 'nothing was done'", async () => {
    const h = await withCard(fixture("confirm", "outcome-unknown"), fixture("confirm", "outcome-unknown"));
    await press(h);
    await h.timers.advance(CONFIRM_AUTO_RETRY_MS);
    h.widget.notNow(ACTION);
    assert.equal(card(h).status?.text, SAFETY.cardUnknownHelp);
  });
});

describe("after an unknown outcome, never 'not done' (leo's review)", () => {
  for (const [label, first, second] of [
    ["outcome_unknown, then confirm_failed", fixture("confirm", "outcome-unknown"), fixture("confirm", "confirm-failed")],
    ["a network error, then limits_unavailable", "network", fixture("confirm", "limits-unavailable")],
    ["outcome_unknown, then 404 widget_not_found", fixture("confirm", "outcome-unknown"), fixture("confirm", "widget-not-found")],
  ] as const) {
    test(`${label}: 'We could not check yet', the same token, a manual retry`, async () => {
      const h = await withCard(first, second, fixture("confirm", "executed"));
      await press(h);
      await h.timers.advance(CONFIRM_AUTO_RETRY_MS);
      assert.equal(confirmCalls(h).length, 2);
      assert.equal(h.state().cards[ACTION].status, "check_manual");
      assert.equal(card(h).status?.text, SAFETY.cardStillChecking);
      assert.deepEqual(card(h).confirm, { label: { text: "Try again", lang: "en" }, enabled: true });
      assertNeverNothing(h);
      await h.timers.advance(10 * 60_000);
      assert.equal(confirmCalls(h).length, 2, "no further automatic retry");
      await press(h);
      assert.equal(card(h).status?.text, EXECUTED);
      assertSameToken(h);
    });
  }

  test("outcome_unknown, then a 429: the countdown keeps the unknown-outcome line", async () => {
    const h = await withCard(fixture("confirm", "outcome-unknown"), fixture("confirm", "rate-limited"), fixture("confirm", "executed"));
    await press(h);
    await h.timers.advance(CONFIRM_AUTO_RETRY_MS);
    assert.equal(h.state().cards[ACTION].status, "rate_limited");
    assert.equal(card(h).extra?.text, SAFETY.cardUnknownHelp);
    assertNeverNothing(h);
    await h.timers.advance(9_000);
    assert.equal(card(h).extra?.text, SAFETY.cardUnknownHelp);
    await press(h);
    assert.equal(card(h).status?.text, EXECUTED);
    assertSameToken(h);
  });

  test("a 401 from 'Talk to a person' while a confirm is in flight: the cut-off confirm is an unknown outcome", async () => {
    const api = new FakeApi()
      .on(ROUTE.session, fixture("sessions", "created"))
      .on(ROUTE.message, fixture("messages", "confirmation-book"))
      .on(ROUTE.handoff, fixture("handoffs", "unauthorized"))
      .on(CONFIRM, "hang");
    const h = await openWidget({ api });
    await h.say("Book me in");
    void h.widget.confirm(ACTION);
    await settle();
    await h.widget.talkToPerson();
    assert.equal(h.view().notice?.kind, "session_expired");
    assert.equal(card(h).status?.text, SAFETY.cardSessionEnded);
    assert.equal(card(h).extra?.text, SAFETY.cardUnknownHelp);
    assertNeverNothing(h);
  });
});

describe("404s: only action_not_found is 'expired' (leo's review, PR 2b)", () => {
  test("404 widget_not_found first: not 'Expired', Try again offered, the same token later", async () => {
    const h = await withCard(fixture("confirm", "widget-not-found"), fixture("confirm", "executed"));
    await press(h);
    assert.notEqual(card(h).status?.text, SAFETY.cardExpired);
    assert.equal(card(h).status?.text, SAFETY.cardNotDone);
    assert.deepEqual(card(h).confirm, { label: { text: "Try again", lang: "en" }, enabled: true });
    await h.timers.advance(60_000);
    assert.equal(confirmCalls(h).length, 1, "no automatic retry: it ran nothing");
    await press(h);
    assert.equal(card(h).status?.text, EXECUTED);
    assertSameToken(h);
  });

  test("a 404 whose body it cannot read ran nothing either: not 'Expired'", async () => {
    const h = await withCard({ status: 404, model: "none", body: null, text: "Not Found" });
    await press(h);
    assert.equal(card(h).status?.text, SAFETY.cardNotDone);
  });

  test("503 session_store_unavailable spends the one automatic retry ('We are checking': conservative)", async () => {
    const h = await withCard(fixture("confirm", "session-store-unavailable"), fixture("confirm", "executed"));
    await press(h);
    assert.equal(card(h).status?.text, SAFETY.cardChecking);
    await h.timers.advance(CONFIRM_AUTO_RETRY_MS);
    assert.equal(card(h).status?.text, EXECUTED);
    assertSameToken(h);
  });
});

describe("End chat and a confirm (leo's review)", () => {
  test("End chat is disabled while a confirm is in flight or being checked, and does nothing", async () => {
    const h = await withCard("hang", fixture("confirm", "outcome-unknown"));
    void h.widget.confirm(ACTION);
    await settle();
    assert.equal(h.view().endChat.enabled, false, "in flight");
    await h.widget.endChat();
    assert.equal(h.view().phase, "chat");
    await h.timers.advance(15_000); // the session's confirm timeout: the automatic retry pending
    assert.equal(h.state().cards[ACTION].status, "checking");
    assert.equal(h.view().endChat.enabled, false, "checking");
    await h.widget.endChat();
    assert.equal(h.view().phase, "chat");
    await h.timers.advance(CONFIRM_AUTO_RETRY_MS);
    assert.equal(h.state().cards[ACTION].status, "check_manual");
    assert.equal(h.view().endChat.enabled, true);
    assert.equal(h.api.count(ROUTE.end), 0);
  });

  test("End chat while the outcome is unknown: the ended view says so, never 'nothing was done'", async () => {
    const h = await withCard(fixture("confirm", "outcome-unknown"), fixture("confirm", "outcome-unknown"));
    h.api.on(ROUTE.end, fixture("end-session", "revoked"));
    await press(h);
    await h.timers.advance(CONFIRM_AUTO_RETRY_MS);
    await h.widget.endChat();
    assert.equal(h.view().phase, "ended");
    assert.deepEqual(h.view().ended?.unknownAction, { text: SAFETY.cardUnknownHelp, lang: "en" });
    assertNeverNothing(h);
    h.widget.startNew();
    assert.equal(h.state().endedWithUnknownAction, false);
  });

  test("End chat after an unknown outcome was settled (executed): no unknown-outcome line", async () => {
    const h = await withCard(fixture("confirm", "outcome-unknown"), fixture("confirm", "executed"));
    h.api.on(ROUTE.end, fixture("end-session", "revoked"));
    await press(h);
    await h.timers.advance(CONFIRM_AUTO_RETRY_MS);
    assert.equal(card(h).status?.text, EXECUTED);
    await h.widget.endChat();
    assert.equal(h.view().ended?.unknownAction, null);
  });
});

describe("after an urgent hand-off ([proposal] (a), leo's and zara's review; D11)", () => {
  test("a card offered later arrives dropped: its token is never kept or sent", async () => {
    const api = new FakeApi()
      .on(ROUTE.session, fixture("sessions", "created"))
      .on(ROUTE.message, fixture("messages", "handoff-safety"), fixture("messages", "confirmation-book"))
      .on(CONFIRM, fixture("confirm", "executed"));
    const h = await openWidget({ api });
    await h.say("I feel faint");
    await h.say("Book me in");
    assert.equal(h.state().cards[ACTION].status, "dropped");
    assert.equal(card(h).status?.text, SAFETY.cardDropped);
    assert.equal(card(h).confirm, null);
    assert.equal(card(h).notNow, null);
    await h.widget.confirm(ACTION);
    assert.equal(confirmCalls(h).length, 0);
  });

  test("a new chat can confirm again", async () => {
    const api = new FakeApi()
      .on(ROUTE.session, fixture("sessions", "created"), fixture("sessions", "created"))
      .on(ROUTE.message, fixture("messages", "handoff-safety"), fixture("messages", "confirmation-book"))
      .on(ROUTE.end, fixture("end-session", "revoked"))
      .on(CONFIRM, fixture("confirm", "executed"));
    const h = await openWidget({ api });
    await h.say("I feel faint");
    await h.widget.endChat();
    h.widget.startNew();
    await h.say("Book me in");
    assert.equal(h.state().cards[ACTION].status, "ready");
    await press(h);
    assert.equal(card(h).status?.text, EXECUTED);
  });
});

describe("the card's countdown (leo's review)", () => {
  test("the first number after an idle page counts from the answer, not from the last tick", async () => {
    const h = await withCard(fixture("confirm", "rate-limited"));
    await h.timers.advance(5 * 60_000);
    await press(h);
    assert.equal(card(h).countdown?.text, "Try again in 9 s.");
  });
});

describe("S13: typing 'yes' is a new turn (leo's review)", () => {
  test("it posts {message: 'yes'} only; no confirm, the card stays ready, its token unused", async () => {
    const api = new FakeApi()
      .on(ROUTE.session, fixture("sessions", "created"))
      .on(ROUTE.message, fixture("messages", "confirmation-book"), fixture("messages", "reply"))
      .on(CONFIRM, fixture("confirm", "executed"));
    const h = await openWidget({ api });
    await h.say("Book me in on Tuesday");
    await h.say("yes");
    const messages = h.api.calls.filter((call) => `${call.method} ${call.path}` === ROUTE.message);
    assert.deepEqual(messages[1].body, { message: "yes" });
    const sent = JSON.stringify(messages.map((call) => call.body));
    assert.ok(!sent.includes(CONFIRM_TOKEN) && !sent.includes(ACTION), "no token or action id in a message");
    assert.equal(confirmCalls(h).length, 0);
    assert.equal(h.state().cards[ACTION].status, "ready");
    await press(h);
    assertSameToken(h);
  });
});

describe("S16: a confirm that is not definite shows the guidance and the contact (zara's review)", () => {
  for (const [label, reply] of [
    ["a network error", "network"],
    ["503 outcome_unknown", fixture("confirm", "outcome-unknown")],
    ["503 confirm_failed", fixture("confirm", "confirm-failed")],
    ["503 limits_unavailable", fixture("confirm", "limits-unavailable")],
    ["404 widget_not_found", fixture("confirm", "widget-not-found")],
    ["429", fixture("confirm", "rate-limited")],
  ] as const) {
    test(`${label}: the guidance first, the contact`, async () => {
      const h = await withCard(reply, reply);
      await press(h);
      assertGuidanceAndContact(h);
    });
  }

  test("a client timeout: the guidance and the contact", async () => {
    const h = await withCard("hang", "hang");
    void h.widget.confirm(ACTION);
    await settle();
    await h.timers.advance(15_000);
    assertGuidanceAndContact(h);
  });

  test("a 429's own guidance wins over the cached copy", async () => {
    const limited = fixture("confirm", "rate-limited");
    (limited.body as { guidance: string[] }).guidance = ["A line only the confirm 429 carries."];
    const h = await withCard(limited);
    await press(h);
    assertGuidanceAndContact(h, ["A line only the confirm 429 carries."]);
  });

  test("a definite answer after it clears the guidance again", async () => {
    const h = await withCard(fixture("confirm", "outcome-unknown"), fixture("confirm", "executed"));
    await press(h);
    assertGuidanceAndContact(h);
    await h.timers.advance(CONFIRM_AUTO_RETRY_MS);
    assert.equal(card(h).status?.text, EXECUTED);
    assert.equal(h.view().notice, null);
    assert.equal(h.view().guidance, null);
  });

  for (const name of ["executed", "failed", "not-found"]) {
    test(`a definite ${name}: no guidance`, async () => {
      const h = await withCard(fixture("confirm", name));
      await press(h);
      assert.equal(h.view().notice, null);
      assert.equal(h.view().guidance, null);
    });
  }
});

// ── Mutation probes (M8 PR 4 review) ──────────────────────────────────────────────────────────

describe("one request at a time (mutation review)", () => {
  test("a confirm in flight, then being checked, blocks Send; the manual retry state does not", async () => {
    const h = await withCard("hang", fixture("confirm", "outcome-unknown"));
    void h.widget.confirm(ACTION);
    await settle();
    h.widget.setDraft("Hello");
    assert.equal(h.view().composer?.canSend, false, "in flight");
    await h.widget.send();
    assert.equal(h.api.count(ROUTE.message), 1);
    await h.timers.advance(15_000); // the session's confirm timeout: 'checking', the retry pending
    assert.equal(h.state().cards[ACTION].status, "checking");
    assert.equal(h.view().composer?.canSend, false, "checking");
    await h.widget.send();
    assert.equal(h.api.count(ROUTE.message), 1);
    await h.timers.advance(CONFIRM_AUTO_RETRY_MS);
    assert.equal(h.state().cards[ACTION].status, "check_manual");
    assert.equal(h.view().composer?.canSend, true);
  });

  test("a message in flight disables a ready card's buttons, and confirm() sends nothing", async () => {
    const api = new FakeApi()
      .on(ROUTE.session, fixture("sessions", "created"))
      .on(ROUTE.message, fixture("messages", "confirmation-book"), "hang")
      .on(CONFIRM, fixture("confirm", "executed"));
    const h = await openWidget({ api });
    await h.say("Book me in");
    h.widget.setDraft("Wait, one more thing");
    void h.widget.send();
    await settle();
    assert.equal(card(h).confirm?.enabled, false);
    assert.equal(card(h).notNow?.enabled, false);
    await h.widget.confirm(ACTION);
    assert.equal(confirmCalls(h).length, 0);
  });

  test("a 200 whose body says 'failed' cannot be read: 'We are checking', never done", async () => {
    const contradictory = fixture("confirm", "failed");
    contradictory.status = 200;
    const h = await withCard(contradictory, fixture("confirm", "executed"));
    await press(h);
    assert.equal(card(h).status?.text, SAFETY.cardChecking);
  });
});

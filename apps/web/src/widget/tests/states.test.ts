/**
 * The widget's states (M8 PR 4): S8, S9, S14, S16, S18, D11 and D15 through the controller, with
 * the API scripted from `fixtures/widget/`.
 */

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { en } from "../i18n/en.ts";
import { SAFETY } from "../i18n/safety.ts";
import { tokenKey, tokenStore } from "../core/storage.ts";
import { DEFAULT_ACCENT } from "../core/view.ts";
import { FakeApi, Held, ROUTE, SUNRISE, SpyStorage, fixture, openWidget, settle, shownTexts } from "./harness.ts";
import type { Harness } from "./harness.ts";

interface ConfigBody {
  guidance: string[];
  texts: { language: string; contact_fallback: string[]; handoff_follow_up?: string; urgent_follow_up?: string }[];
}

const SUNRISE_CONFIG = fixture("config", "sunrise-clinic").body as ConfigBody;
const GUIDANCE = SUNRISE_CONFIG.guidance;
const CONTACT = SUNRISE_CONFIG.texts[0].contact_fallback;
const FOLLOW_UPS = (fixture("config", "sunrise-clinic-follow-ups").body as ConfigBody).texts[0];
const HANDOFF_FOLLOW_UP = FOLLOW_UPS.handoff_follow_up!;
const URGENT_FOLLOW_UP = FOLLOW_UPS.urgent_follow_up!;
const TOKEN = (fixture("sessions", "created").body as { token: string }).token;
const HANDOFF_ID = "6f1c2a9e-0d4b-4c8e-9a51-2b7e3f4a5c61";
const ACTION = "3d6f0a52-8b1e-4f7a-9c2d-5e8b1a7f4c30";

function withSession(api: FakeApi = new FakeApi(), businessId: string = SUNRISE): FakeApi {
  const created = fixture("sessions", "created");
  const body = created.body as { token: string };
  body.token = body.token.replace(`${SUNRISE}.`, `${businessId}.`); // S3: the token names it
  return api.on(ROUTE.session, created);
}

function guidanceOf(h: Harness): string[] | undefined {
  return h.view().guidance?.lines.map((line) => line.text);
}

/** S16 and S8: the guidance, verbatim, in order, in English, first in the status area. */
function assertGuidanceFirst(h: Harness, expected: string[] = GUIDANCE): void {
  const view = h.view();
  assert.ok(view.guidance, "the guidance is shown");
  assert.deepEqual(guidanceOf(h), expected);
  assert.ok(view.guidance.lines.every((line) => line.lang === "en"));
  assert.equal(view.guidance.heading.lang, "en");
}

/** zara's review: the urgent heading only for an urgent hand-off (and pinned after it). */
function assertHeading(h: Harness, urgent: boolean): void {
  const guidance = h.view().guidance;
  assert.ok(guidance, "the guidance is shown");
  assert.equal(guidance.urgent, urgent);
  assert.equal(guidance.heading.text, urgent ? SAFETY.guidanceHeadingUrgent : SAFETY.guidanceHeading);
}

function assertContact(h: Harness, lines: readonly string[] = CONTACT): void {
  const texts = shownTexts(h.view());
  for (const line of lines) assert.ok(texts.includes(line), `contact line: ${line}`);
}

function customerStatus(h: Harness): string | undefined {
  const first = h.view().transcript[0];
  return first?.kind === "customer" ? first.status?.text : undefined;
}

function assertNoPersonWillReply(h: Harness): void {
  const texts = shownTexts(h.view());
  assert.ok(!texts.includes(HANDOFF_FOLLOW_UP), "no 'a person will reply' text");
}

describe("S14: the session is created lazily", () => {
  test("opening the widget fetches the configuration and creates no session", async () => {
    const h = await openWidget();
    assert.deepEqual(
      h.api.calls.map((call) => `${call.method} ${call.path}`),
      [ROUTE.config(SUNRISE)],
    );
    assert.equal(h.state().hasSession, false);
    assert.equal(h.storage.writes.length, 0);
    const view = h.view();
    assert.equal(view.phase, "chat");
    assert.equal(view.aiDisclosure.lang, "en");
    assert.equal(view.talkToPerson.visible, true);
    assert.equal(view.guidance, null, "the greeting is not a failure: no guidance yet");
  });

  test("the first send creates one session; later sends reuse it", async () => {
    const api = withSession().always(ROUTE.message, fixture("messages", "reply"));
    const h = await openWidget({ api });
    await h.say("Hello");
    await h.say("And again");
    assert.equal(h.api.count(ROUTE.session), 1);
    assert.equal(h.api.count(ROUTE.message), 2);
    const create = h.api.calls.find((call) => `${call.method} ${call.path}` === ROUTE.session);
    assert.deepEqual(create?.body, { business_id: SUNRISE, ui_language: "en" });
    const message = h.api.calls.find((call) => `${call.method} ${call.path}` === ROUTE.message);
    assert.equal(message?.headers.authorization, `Bearer ${TOKEN}`);
    assert.deepEqual(message?.body, { message: "Hello" });
    assert.equal(message?.init.credentials, "omit");
    assert.equal(message?.init.redirect, "error");
    assert.equal(message?.init.cache, "no-store");
    assert.equal(message?.init.mode, undefined, "default mode, so a POST keeps its Origin");
  });

  test("a send and 'Talk to a person' pressed together share one session", async () => {
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "reply"))
      .on(ROUTE.handoff, fixture("handoffs", "created"));
    const h = await openWidget({ api });
    h.widget.setDraft("Hello");
    const sent = h.widget.send();
    const asked = h.widget.talkToPerson();
    await settle();
    await Promise.all([sent, asked]);
    assert.equal(h.api.count(ROUTE.session), 1);
  });

  test("the session is created in the chosen UI language", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "reply"));
    const h = await openWidget({ api, lang: "hi" });
    await h.say("नमस्ते");
    const create = h.api.calls.find((call) => `${call.method} ${call.path}` === ROUTE.session);
    assert.deepEqual(create?.body, { business_id: SUNRISE, ui_language: "hi" });
  });

  test("a lang the business does not offer falls back to its first language", async () => {
    const h = await openWidget({ lang: "te" });
    assert.equal(h.state().uiLanguage, "en");
  });
});

describe("S16: guidance first in every state that is not a success", () => {
  test("network error: guidance, contact, the text kept, 'may still be processed'", async () => {
    const h = await openWidget({ api: withSession().on(ROUTE.message, "network") });
    await h.say("When are you open?");
    assert.equal(h.view().notice?.kind, "network_error");
    assertGuidanceFirst(h);
    assert.equal(h.state().draft, "When are you open?");
    const texts = shownTexts(h.view());
    assert.ok(texts.includes(en.mayStillBeProcessed));
    assert.ok(texts.includes(en.messageKept));
    for (const line of CONTACT) assert.ok(texts.includes(line));
  });

  test("client timeout: after the session's own message timeout, never a hard-coded one", async () => {
    const h = await openWidget({ api: withSession().on(ROUTE.message, "hang") });
    h.widget.setDraft("Are you open on Sunday?");
    void h.widget.send();
    await settle();
    assert.equal(h.view().notice?.kind, "working");
    await h.timers.advance(64_999);
    assert.equal(h.view().notice?.kind, "working", "the session said 65 s");
    await h.timers.advance(1);
    assert.equal(h.view().notice?.kind, "timeout");
    assertGuidanceFirst(h);
    assert.ok(shownTexts(h.view()).includes(en.mayStillBeProcessed));
    assert.equal(h.state().draft, "Are you open on Sunday?");
  });

  test("401: session expired, the token forgotten, the text kept", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "unauthorized"));
    const h = await openWidget({ api });
    await h.say("Hello");
    assert.equal(h.view().notice?.kind, "session_expired");
    assertGuidanceFirst(h);
    assert.equal(h.state().hasSession, false);
    assert.equal(h.state().draft, "Hello");
    assert.deepEqual(h.storage.keys(), []);
  });

  test("422: invalid message", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "invalid"));
    const h = await openWidget({ api });
    await h.say("Hello");
    assert.equal(h.view().notice?.kind, "invalid_message");
    assertGuidanceFirst(h);
    assert.equal(h.state().draft, "Hello");
  });

  test("429: the body's guidance and a countdown from Retry-After; Send stays available", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "rate-limited"));
    const h = await openWidget({ api });
    await h.say("Hello");
    assert.equal(h.view().notice?.kind, "rate_limited");
    assertGuidanceFirst(h);
    assert.equal(h.view().notice?.countdown?.text, "You can send again in 12 s.");
    assert.equal(h.view().composer?.canSend, true, "the server's limiter decides (zara's review)");
    await h.timers.advance(5_000);
    assert.equal(h.view().notice?.countdown?.text, "You can send again in 7 s.");
    await h.timers.advance(7_000);
    assert.equal(h.view().notice?.countdown, null);
    assert.ok(shownTexts(h.view()).includes(en.canSendAgain));
    assert.equal(h.view().composer?.canSend, true);
  });

  test("a message sent during a 429 countdown reaches the API (a red flag skips the limiter, M7 G1)", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "rate-limited"), fixture("messages", "handoff-safety"));
    const h = await openWidget({ api });
    await h.say("Hello");
    assert.equal(h.view().notice?.countdown?.text, "You can send again in 12 s.");
    await h.say("This is an emergency");
    assert.equal(h.api.count(ROUTE.message), 2);
    assert.equal(h.view().notice?.kind, "handoff");
    assertHeading(h, true);
  });

  test("429: the body's guidance wins over the cached copy", async () => {
    const limited = fixture("messages", "rate-limited");
    (limited.body as { guidance: string[] }).guidance = ["A line only the 429 body carries."];
    const h = await openWidget({ api: withSession().on(ROUTE.message, limited) });
    await h.say("Hello");
    assertGuidanceFirst(h, ["A line only the 429 body carries."]);
  });

  test("429 without guidance in its body: the cached copy from the config route", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "rate-limited-no-guidance"));
    const h = await openWidget({ api });
    await h.say("Hello");
    assertGuidanceFirst(h);
  });

  test("5xx: server error, the outcome unknown", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "server-error"));
    const h = await openWidget({ api });
    await h.say("Hello");
    assert.equal(h.view().notice?.kind, "server_error");
    assertGuidanceFirst(h);
    assert.ok(shownTexts(h.view()).includes(en.mayStillBeProcessed));
    assert.equal(customerStatus(h), en.mayBeProcessed);
  });

  test("404 widget_not_found: nothing ran, so 'not sent'", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "widget-not-found"));
    const h = await openWidget({ api });
    await h.say("Hello");
    assert.equal(h.view().notice?.kind, "server_error");
    assertGuidanceFirst(h);
    assert.ok(!shownTexts(h.view()).includes(en.mayStillBeProcessed));
    assert.equal(customerStatus(h), en.notSent);
    assert.equal(h.state().draft, "Hello");
  });

  test("503 from the session lookup: nothing ran, so 'not sent'", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "store-unavailable"));
    const h = await openWidget({ api });
    await h.say("Hello");
    assert.equal(h.view().notice?.kind, "server_error");
    assertGuidanceFirst(h);
    assert.ok(!shownTexts(h.view()).includes(en.mayStillBeProcessed));
    assert.equal(customerStatus(h), en.notSent);
  });

  for (const name of ["unknown-kind", "malformed"]) {
    test(`an unreadable turn (${name}) is the safe hand-off state`, async () => {
      const api = withSession().on(ROUTE.message, fixture("messages", name));
      const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
      await h.say("Hello");
      assert.equal(h.view().notice?.kind, "unknown_kind");
      assertGuidanceFirst(h);
      assertNoPersonWillReply(h);
      assert.ok(!shownTexts(h.view()).includes(URGENT_FOLLOW_UP));
      for (const line of CONTACT) assert.ok(shownTexts(h.view()).includes(line));
      await h.timers.advance(60_000);
      assert.equal(h.api.calls.filter((call) => call.method === "GET").length, 1, "no polling");
    });
  }

  for (const [name, error] of [
    ["limits-unavailable", "Redis down"],
    ["store-unavailable", "Postgres down"],
  ] as const) {
    test(`failed session creation (${error}): guidance and contact, the text kept`, async () => {
      const api = new FakeApi().on(ROUTE.session, fixture("sessions", name));
      const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
      await h.say("Hello");
      assert.equal(h.view().notice?.kind, "session_unavailable");
      assertGuidanceFirst(h);
      for (const line of CONTACT) assert.ok(shownTexts(h.view()).includes(line));
      assertNoPersonWillReply(h);
      assert.equal(h.state().draft, "Hello");
      assert.equal(h.api.count(ROUTE.message), 0);
    });
  }

  test("failed session creation (429): the body's guidance and a countdown", async () => {
    const api = new FakeApi().on(ROUTE.session, fixture("sessions", "rate-limited"));
    const h = await openWidget({ api });
    await h.say("Hello");
    assert.equal(h.view().notice?.kind, "rate_limited");
    assertGuidanceFirst(h);
    assert.equal(h.view().notice?.countdown?.text, "You can send again in 20 s.");
  });

  test("failed session creation (network): guidance, and nothing 'may still be processed'", async () => {
    const api = new FakeApi().on(ROUTE.session, "network");
    const h = await openWidget({ api });
    await h.say("Hello");
    assert.equal(h.view().notice?.kind, "network_error");
    assertGuidanceFirst(h);
    assert.ok(!shownTexts(h.view()).includes(en.mayStillBeProcessed));
  });

  test("a business without guidance shows none, and still its contact", async () => {
    const api = new FakeApi().on(ROUTE.session, "network");
    const h = await openWidget({ api, businessId: "fixture-shop", config: fixture("config", "fixture-shop") });
    await h.say("Hello");
    assert.equal(h.view().notice?.kind, "network_error");
    assert.equal(h.view().guidance, null);
    assert.ok(shownTexts(h.view()).includes("E-mail: help@fixture-shop.example"));
  });

  test("success states show no guidance", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "answer"));
    const h = await openWidget({ api });
    await h.say("When are you open?");
    assert.equal(h.view().notice, null);
    assert.equal(h.view().guidance, null);
  });
});

describe("S14: Redis down at the first message", () => {
  test("guidance and contact fallback, never 'a person will reply', no message sent", async () => {
    const api = new FakeApi().on(ROUTE.session, fixture("sessions", "limits-unavailable"));
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    await h.say("My chest hurts and I cannot breathe");
    assertGuidanceFirst(h);
    const texts = shownTexts(h.view());
    for (const line of CONTACT) assert.ok(texts.includes(line), `contact line: ${line}`);
    assert.ok(!texts.includes(HANDOFF_FOLLOW_UP));
    assert.ok(!texts.includes(URGENT_FOLLOW_UP));
    assert.equal(h.api.count(ROUTE.message), 0);
  });
});

describe("S9: answers only with sources", () => {
  test("an answered answer with sources is shown with its citations", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "answer"));
    const h = await openWidget({ api });
    await h.say("When are you open?");
    const entry = h.view().transcript.at(-1);
    assert.equal(entry?.kind, "answer");
    assert.equal(entry?.kind === "answer" && entry.sources.length, 2);
    assert.equal(entry?.kind === "answer" && entry.sources[0].title.text, "Opening hours");
    assert.equal(entry?.kind === "answer" && entry.who.lang, "en");
  });

  for (const name of ["answer-no-sources", "answer-dont-know"]) {
    test(`${name}: no answer is shown; guidance and contact instead`, async () => {
      const api = withSession().on(ROUTE.message, fixture("messages", name));
      const h = await openWidget({ api });
      await h.say("When are you open?");
      assert.ok(h.view().transcript.every((entry) => entry.kind !== "answer"));
      assert.ok(!shownTexts(h.view()).includes("An answer that cites nothing must never be shown."));
      assert.equal(h.view().notice?.kind, "no_answer");
      assertGuidanceFirst(h);
    });
  }

  test("a reply carries its provenance label, in English", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "reply"));
    const h = await openWidget({ api });
    await h.say("I want an appointment");
    const entry = h.view().transcript.at(-1);
    assert.equal(entry?.kind, "reply");
    assert.deepEqual(entry?.kind === "reply" && entry.provenance, { text: SAFETY.replyProvenance, lang: "en" });
  });
});

describe("S8, D11: honest hand-offs", () => {
  async function handoff(name: string) {
    const api = withSession()
      .on(ROUTE.message, fixture("messages", name))
      .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
    return openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") }).then(
      async (h) => {
        await h.say("Hello");
        return h;
      },
    );
  }

  for (const name of [
    "handoff-urgent-guard-error",
    "handoff-safety",
    "handoff-turn-timeout",
    "handoff-guards-not-configured",
    "xss-handoff",
  ]) {
    test(`${name}: urgent never renders handoff_follow_up; D11's text instead`, async () => {
      const h = await handoff(name);
      const notice = h.view().notice;
      assert.equal(notice?.kind, "handoff");
      assert.ok(h.view().guidance);
      assertHeading(h, true);
      assertNoPersonWillReply(h);
      assert.deepEqual(notice?.followUp, { text: URGENT_FOLLOW_UP, lang: "en" });
      // S8 as decided: the contact fallback too; neither a reply prompt nor an ETA (zara's review).
      assertContact(h);
      await h.timers.advance(60_000);
      assert.equal(h.api.calls.filter((call) => call.path.startsWith("/v1/widget/handoffs/")).length, 0, "no polling");
    });
  }

  test("a safety reason is urgent whatever its priority says (S8)", async () => {
    const safety = fixture("messages", "handoff-safety");
    (safety.body as { turn: { priority: string } }).turn.priority = "normal";
    const api = withSession().on(ROUTE.message, safety).always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    await h.say("Hello");
    assertNoPersonWillReply(h);
    assert.deepEqual(h.view().notice?.followUp, { text: URGENT_FOLLOW_UP, lang: "en" });
    await h.timers.advance(60_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 0, "no polling");
  });

  test("urgent and not stored: no follow-up at all, the contact fallback", async () => {
    const h = await handoff("handoff-safety-not-stored");
    assertGuidanceFirst(h);
    assert.equal(h.view().notice?.followUp, null);
    assertNoPersonWillReply(h);
    for (const line of CONTACT) assert.ok(shownTexts(h.view()).includes(line));
  });

  test("urgent with no urgent_follow_up configured: no follow-up text, the contact fallback", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "handoff-safety"));
    const h = await openWidget({ api });
    await h.say("Hello");
    assert.equal(h.view().notice?.followUp, null);
    assert.deepEqual(h.view().notice?.status, []);
    assertContact(h);
  });

  test("an urgent hand-off at a business without guidance is never a dead end (zara's review)", async () => {
    // Platform red flags apply to every business type; a non-health business gets guidance [].
    const urgent = fixture("messages", "handoff-safety");
    (urgent.body as { turn: { guidance: string[] } }).turn.guidance = [];
    const api = withSession(new FakeApi(), "fixture-shop").on(ROUTE.message, urgent).always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
    const h = await openWidget({ api, businessId: "fixture-shop", config: fixture("config", "fixture-shop") });
    await h.say("This is an emergency, someone collapsed in your store");
    assert.equal(h.view().notice?.kind, "handoff");
    assert.equal(h.view().guidance, null, "a business without guidance shows none (S8)");
    assert.equal(h.view().notice?.followUp, null);
    assert.ok(!shownTexts(h.view()).includes("Fixture text: a member of our team will reply here."));
    assertContact(h, ["E-mail: help@fixture-shop.example"]);
    await h.timers.advance(60_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 0, "no polling");
  });

  test("normal and stored: the business's follow-up, then polling for a reply (D7 (a))", async () => {
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "handoff-normal-queued"))
      .on(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"), fixture("handoff-status", "replied"));
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    await h.say("I want to talk to someone");
    assert.deepEqual(h.view().notice?.followUp, { text: HANDOFF_FOLLOW_UP, lang: "en" });
    await h.timers.advance(5_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 1);
    await h.timers.advance(5_000);
    const last = h.view().transcript.at(-1);
    assert.equal(last?.kind, "staff_reply");
    await h.timers.advance(30_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 2, "polling stops after the reply");
  });

  test("normal but not stored: no follow-up, the contact fallback, no polling", async () => {
    const h = await handoff("handoff-normal-not-queued");
    assert.equal(h.view().notice?.followUp, null);
    for (const line of CONTACT) assert.ok(shownTexts(h.view()).includes(line));
    await h.timers.advance(30_000);
    assert.equal(h.api.calls.filter((call) => call.path.startsWith("/v1/widget/handoffs/")).length, 0);
  });

  test("a later urgent turn on the same session hides the follow-up and stops polling", async () => {
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "handoff-normal-queued"), fixture("messages", "handoff-safety"))
      .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    await h.say("I want to talk to someone");
    await h.timers.advance(5_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 1, "polling while normal");
    await h.say("Now I feel faint");
    assertNoPersonWillReply(h);
    assert.ok(h.view().guidance, "the guidance is shown");
    await h.timers.advance(30_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 1, "no polling once urgent");
  });

  for (const [label, reply] of [
    ["a timeout", "hang"],
    ["a network error", "network"],
    ["a 5xx", fixture("messages", "server-error")],
  ] as const) {
    test(`after ${label} on a message, polling stops and no later follow-up (the row may be urgent)`, async () => {
      const api = withSession()
        .on(ROUTE.message, fixture("messages", "handoff-normal-queued"), reply)
        .on(ROUTE.handoff, fixture("handoffs", "created"))
        .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
      const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
      await h.say("I want to talk to someone");
      await h.timers.advance(5_000);
      assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 1);
      h.widget.setDraft("My chest hurts");
      void h.widget.send(); // not awaited: a hanging request ends only when the clock moves
      await settle();
      await h.timers.advance(65_000); // past the session's message timeout, for the hang
      const polls = h.api.count(ROUTE.status(HANDOFF_ID));
      await h.timers.advance(60_000);
      assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), polls, "no polling");
      await h.widget.talkToPerson();
      assert.equal(h.view().notice?.kind, "handoff");
      assert.equal(h.view().notice?.followUp, null);
      assertNoPersonWillReply(h);
      assertGuidanceFirst(h);
      for (const line of CONTACT) assert.ok(shownTexts(h.view()).includes(line));
      await h.timers.advance(60_000);
      assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), polls, "still no polling");
    });
  }

  test("a 503 from the session lookup ran nothing: the follow-up stays possible", async () => {
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "store-unavailable"))
      .on(ROUTE.handoff, fixture("handoffs", "created"))
      .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    await h.say("Hello");
    await h.widget.talkToPerson();
    assert.deepEqual(h.view().notice?.followUp, { text: HANDOFF_FOLLOW_UP, lang: "en" });
  });

  test("resolved without a reply: polling stops and the follow-up gives way to the contact", async () => {
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "handoff-normal-queued"))
      .on(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "claimed"), fixture("handoff-status", "resolved"));
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    await h.say("I want to talk to someone");
    await h.timers.advance(10_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 2);
    assertNoPersonWillReply(h);
    for (const line of CONTACT) assert.ok(shownTexts(h.view()).includes(line));
    await h.timers.advance(60_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 2, "polling stopped");
  });

  for (const [name, polls] of [
    ["rate-limited", 3],
    ["limits-unavailable", 3],
  ] as const) {
    test(`a poll answered ${name} is tried again at the next tick, never faster`, async () => {
      const api = withSession()
        .on(ROUTE.message, fixture("messages", "handoff-normal-queued"))
        .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", name));
      const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
      await h.say("I want to talk to someone");
      await h.timers.advance(15_000);
      assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), polls);
      assert.deepEqual(h.view().notice?.followUp, { text: HANDOFF_FOLLOW_UP, lang: "en" });
    });
  }

  test("a poll answered 404 stops polling", async () => {
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "handoff-normal-queued"))
      .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "not-found"));
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    await h.say("I want to talk to someone");
    await h.timers.advance(30_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 1);
  });

  test("not_covered_language: the guidance and the reason, in the UI's language", async () => {
    const h = await handoff("handoff-not-covered");
    assertGuidanceFirst(h);
    assert.ok(shownTexts(h.view()).includes(en.notCoveredLanguage));
  });

  test("an urgent hand-off drops an open confirmation card; its token is never sent", async () => {
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "confirmation-book"), fixture("messages", "handoff-safety"));
    const h = await openWidget({ api });
    await h.say("Book me in");
    await h.say("Now I feel faint");
    const card = h.state().cards["3d6f0a52-8b1e-4f7a-9c2d-5e8b1a7f4c30"];
    assert.equal(card.status, "dropped");
    await h.widget.confirm(card.actionId);
    assert.equal(h.api.calls.filter((call) => call.path.includes("/actions/")).length, 0);
  });

  test("after an urgent hand-off the guidance stays pinned on later successes", async () => {
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "handoff-safety"), fixture("messages", "reply"));
    const h = await openWidget({ api });
    await h.say("Help");
    await h.say("Thank you");
    assert.equal(h.view().notice, null);
    assertGuidanceFirst(h);
  });

  test("'Talk to a person' first: customer_asked, the follow-up, then polling", async () => {
    const api = withSession()
      .on(ROUTE.handoff, fixture("handoffs", "created"))
      .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    await h.widget.talkToPerson();
    const press = h.api.calls.find((call) => `${call.method} ${call.path}` === ROUTE.handoff);
    assert.deepEqual(press?.body, {}, "the business and the customer come from the session (S1)");
    assert.equal(h.view().notice?.kind, "handoff");
    assertGuidanceFirst(h);
    assert.deepEqual(h.view().notice?.followUp, { text: HANDOFF_FOLLOW_UP, lang: "en" });
    await h.timers.advance(5_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 1);
  });

  test("'Talk to a person' after an urgent turn: S17 returns that urgent row; D11's text", async () => {
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "handoff-urgent-guard-error"))
      .on(ROUTE.handoff, fixture("handoffs", "created"));
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    await h.say("Hello");
    await h.widget.talkToPerson();
    assertNoPersonWillReply(h);
    assert.deepEqual(h.view().notice?.followUp, { text: URGENT_FOLLOW_UP, lang: "en" });
    await h.timers.advance(60_000);
    assert.equal(h.api.calls.filter((call) => call.path.startsWith("/v1/widget/handoffs/")).length, 0, "no polling");
  });

  for (const [label, reply] of [
    ["not stored (id null)", fixture("handoffs", "not-stored")],
    ["a body it cannot read", { status: 200, model: "none", body: { id: HANDOFF_ID, queued: "yes" } }],
  ] as const) {
    test(`'Talk to a person' ${label}: guidance and contact, no follow-up, no polling`, async () => {
      const api = withSession().on(ROUTE.handoff, reply);
      const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
      await h.widget.talkToPerson();
      assertGuidanceFirst(h);
      assertNoPersonWillReply(h);
      for (const line of CONTACT) assert.ok(shownTexts(h.view()).includes(line));
      await h.timers.advance(60_000);
      assert.equal(h.api.calls.filter((call) => call.method === "GET").length, 1, "only the config");
    });
  }

  test("'Talk to a person' on a session adopted from sessionStorage: no follow-up (it may be urgent)", async () => {
    const storage = new SpyStorage();
    storage.setItem(tokenKey(SUNRISE), TOKEN);
    const config = fixture("config", "sunrise-clinic-follow-ups");
    // [proposal, PR 2] the config route carries P7's timeouts, so a reload can adopt the token.
    Object.assign(config.body as object, { message_timeout_seconds: 65, confirm_timeout_seconds: 15 });
    const api = new FakeApi().on(ROUTE.handoff, fixture("handoffs", "created"));
    const h = await openWidget({ api, storage, config });
    assert.equal(h.state().hasSession, true, "adopted: the config carried the timeouts");
    await h.widget.talkToPerson();
    assert.equal(h.api.count(ROUTE.session), 0);
    assertNoPersonWillReply(h);
    await h.timers.advance(60_000);
    assert.equal(h.api.calls.filter((call) => call.path.startsWith("/v1/widget/handoffs/")).length, 0);
  });

  test("a stored token is not adopted while the config carries no timeouts (P7)", async () => {
    const storage = new SpyStorage();
    storage.setItem(tokenKey(SUNRISE), TOKEN);
    const h = await openWidget({ storage });
    assert.equal(h.state().hasSession, false);
    assert.deepEqual(storage.keys(), []);
  });
});

describe("S16, D15: the widget never re-sends a message by itself", () => {
  test("after a timeout, no time passing sends it again; the next send is the customer's", async () => {
    const api = withSession().on(ROUTE.message, "hang", fixture("messages", "reply"));
    const h = await openWidget({ api });
    h.widget.setDraft("Is the lab open?");
    void h.widget.send();
    await settle();
    await h.timers.advance(65_000);
    await h.timers.advance(10 * 60_000);
    assert.equal(h.api.count(ROUTE.message), 1);
    assert.deepEqual(h.view().composer?.hint, { text: en.mayStillBeProcessed, lang: "en" });
    await h.widget.send();
    await settle();
    assert.equal(h.api.count(ROUTE.message), 2);
    assert.equal(h.view().composer?.hint, null);
  });

  for (const reply of ["network", fixture("messages", "server-error"), fixture("messages", "rate-limited")] as const) {
    const label = typeof reply === "string" ? reply : String(reply.status);
    test(`after ${label}, nothing is re-sent`, async () => {
      const api = withSession().on(ROUTE.message, reply);
      const h = await openWidget({ api });
      await h.say("Hello");
      await h.timers.advance(10 * 60_000);
      assert.equal(h.api.count(ROUTE.message), 1);
    });
  }

  test("a quick reply is sent only when pressed, as the customer's own text", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "reply"));
    const h = await openWidget({ api });
    await h.timers.advance(60_000);
    assert.equal(h.api.count(ROUTE.message), 0);
    await h.widget.sendQuickReply("What are your opening hours?");
    await settle();
    const message = h.api.calls.find((call) => `${call.method} ${call.path}` === ROUTE.message);
    assert.deepEqual(message?.body, { message: "What are your opening hours?" });
    await h.widget.sendQuickReply("Something the business never offered");
    assert.equal(h.api.count(ROUTE.message), 1);
  });

  test("no send while an IME composition is open", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "reply"));
    const h = await openWidget({ api });
    h.widget.setDraft("నమస్కారం");
    h.widget.compositionStart();
    assert.equal(h.view().composer?.canSend, false);
    await h.widget.send();
    assert.equal(h.api.count(ROUTE.message), 0);
    h.widget.compositionEnd();
    await h.widget.send();
    await settle();
    assert.equal(h.api.count(ROUTE.message), 1);
  });

  test("one message at a time: Send is disabled while one is in flight", async () => {
    const api = withSession().on(ROUTE.message, "hang");
    const h = await openWidget({ api });
    h.widget.setDraft("First");
    void h.widget.send();
    await settle();
    h.widget.setDraft("Second");
    assert.equal(h.view().composer?.canSend, false);
    await h.widget.send();
    assert.equal(h.api.count(ROUTE.message), 1);
  });
});

describe("S18: what the browser keeps", () => {
  test("only the session token is ever written, under its one key; End chat removes it", async () => {
    const storage = new SpyStorage();
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "answer"), fixture("messages", "confirmation-book"))
      .on(ROUTE.confirm("3d6f0a52-8b1e-4f7a-9c2d-5e8b1a7f4c30"), fixture("confirm", "executed"))
      .on(ROUTE.handoff, fixture("handoffs", "created"))
      .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"))
      .on(ROUTE.end, { status: 204, model: "none", body: null });
    const h = await openWidget({ api, storage });
    await h.say("When are you open?");
    await h.say("Book me in, my phone is +91 00000 99999");
    await h.widget.confirm("3d6f0a52-8b1e-4f7a-9c2d-5e8b1a7f4c30");
    await settle();
    await h.widget.talkToPerson();
    await h.timers.advance(10_000);

    assert.deepEqual(storage.writes, [[tokenKey(SUNRISE), TOKEN]]);
    const everything = JSON.stringify({ state: h.state(), view: h.view() });
    assert.ok(!everything.includes(TOKEN), "the session token is never in state or view");
    assert.ok(!everything.includes("Fx7_fixture-token-0001"), "nor the confirm token");

    await h.widget.endChat();
    await settle();
    assert.equal(h.api.count(ROUTE.end), 1);
    const end = h.api.calls.find((call) => call.method === "DELETE");
    assert.equal(end?.headers.authorization, `Bearer ${TOKEN}`);
    assert.deepEqual(storage.keys(), []);
    assert.ok(storage.removes.includes(tokenKey(SUNRISE)));
    assert.deepEqual(h.state().transcript, []);
    assert.deepEqual(h.state().cards, {});
    assert.equal(h.view().phase, "ended");
    assert.equal(h.view().guidance, null, "no urgent hand-off: nothing pinned");
    assert.equal(h.view().ended?.unknownAction, null);
    const polls = h.api.count(ROUTE.status(HANDOFF_ID));
    await h.timers.advance(60_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), polls, "polling stopped");
  });

  test("End chat while a message is in flight: nothing lands in the cleared view", async () => {
    const api = withSession()
      .on(ROUTE.message, "hang")
      .on(ROUTE.end, { status: 204, model: "none", body: null });
    const h = await openWidget({ api });
    h.widget.setDraft("Hello");
    void h.widget.send();
    await settle();
    await h.widget.endChat();
    await settle();
    assert.equal(h.state().draft, "");
    assert.deepEqual(h.state().transcript, []);
    assert.equal(h.state().messageInFlight, false);
    h.widget.startNew();
    assert.equal(h.view().phase, "chat");
    assert.ok(h.view().greeting);
  });

  test("a stored token for another business is never used", async () => {
    const storage = new SpyStorage();
    storage.setItem(tokenKey(SUNRISE), "paws-and-co.some-other-secret");
    const config = fixture("config", "sunrise-clinic");
    Object.assign(config.body as object, { message_timeout_seconds: 65, confirm_timeout_seconds: 15 });
    const h = await openWidget({ storage, config });
    assert.equal(h.state().hasSession, false);
  });

  test("a session response naming another business is refused", async () => {
    const created = fixture("sessions", "created");
    (created.body as { token: string }).token = "paws-and-co.planted";
    const api = new FakeApi().on(ROUTE.session, created);
    const h = await openWidget({ api });
    await h.say("Hello");
    assert.equal(h.view().notice?.kind, "session_unavailable");
    assert.deepEqual(h.storage.writes, []);
  });
});

describe("S8, D11: a row whose priority the widget cannot know yet (zara's review)", () => {
  test("'Talk to a person' answered while a message is in flight: no follow-up, no polling; the message decides", async () => {
    const message = new Held();
    const api = withSession()
      .on(ROUTE.message, message)
      .on(ROUTE.handoff, fixture("handoffs", "created"))
      .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    h.widget.setDraft("My chest hurts and my arm is numb");
    const sent = h.widget.send();
    await settle();
    await h.widget.talkToPerson(); // S17: the row this message may still raise to urgent
    assert.equal(h.view().notice?.kind, "handoff");
    assert.equal(h.view().notice?.followUp, null);
    assertNoPersonWillReply(h);
    assertGuidanceFirst(h);
    assertContact(h);
    await h.timers.advance(30_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 0, "no polling while the message is open");
    message.release(fixture("messages", "handoff-safety"));
    await settle();
    await sent;
    assertHeading(h, true);
    assert.deepEqual(h.view().notice?.followUp, { text: URGENT_FOLLOW_UP, lang: "en" });
    await h.timers.advance(30_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 0, "never polled");
  });

  test("a message in flight when the button's request went out, answered before it: still no follow-up", async () => {
    const message = new Held();
    const button = new Held();
    const api = withSession()
      .on(ROUTE.message, message)
      .on(ROUTE.handoff, button)
      .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    h.widget.setDraft("Hello");
    const sent = h.widget.send();
    await settle();
    const pressed = h.widget.talkToPerson();
    await settle();
    message.release(fixture("messages", "reply"));
    await settle();
    await sent;
    button.release(fixture("handoffs", "created"));
    await settle();
    await pressed;
    assert.equal(h.view().notice?.kind, "handoff");
    assert.equal(h.view().notice?.followUp, null);
    await h.timers.advance(30_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 0);
  });

  test("a message sent after the button's request went out, still open when it answers: no follow-up", async () => {
    const message = new Held();
    const button = new Held();
    const api = withSession()
      .on(ROUTE.message, message)
      .on(ROUTE.handoff, button)
      .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    const pressed = h.widget.talkToPerson();
    await settle();
    h.widget.setDraft("My chest hurts");
    void h.widget.send(); // Send stays available while the button's request runs
    await settle();
    assert.equal(h.api.count(ROUTE.message), 1);
    button.release(fixture("handoffs", "created"));
    await settle();
    await pressed;
    assert.equal(h.view().notice?.kind, "handoff");
    assert.equal(h.view().notice?.followUp, null);
    await h.timers.advance(30_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 0);
  });

  const replyNamingRow = fixture("messages", "reply");
  (replyNamingRow.body as { handoff: unknown }).handoff = { id: HANDOFF_ID, queued: true };
  for (const [label, reply] of [
    [
      "an unreadable hand-off turn naming a stored row",
      {
        status: 200,
        model: "none",
        body: {
          turn: { kind: "handoff", trace_id: "0".repeat(32), reason: "safety", priority: "urgent", guidance: "Call 112 now" },
          handoff: { id: HANDOFF_ID, queued: true },
        },
      },
    ],
    ["a 200 that is not JSON", { status: 200, model: "none", body: null, text: "<html>a proxy's page</html>" }],
    ["a reply turn whose body names a hand-off row", replyNamingRow],
  ] as const) {
    test(`${label}: a later 'Talk to a person' shows no follow-up and never polls`, async () => {
      const api = withSession()
        .on(ROUTE.message, reply)
        .on(ROUTE.handoff, fixture("handoffs", "created"))
        .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
      const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
      await h.say("Hello");
      await h.widget.talkToPerson();
      assert.equal(h.view().notice?.kind, "handoff");
      assert.equal(h.view().notice?.followUp, null);
      assertNoPersonWillReply(h);
      assertGuidanceFirst(h);
      assertContact(h);
      await h.timers.advance(30_000);
      assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 0, "no polling");
    });
  }
});

describe("Z4 (b): a hand-off that carries guidance was not cleared (decided 2026-11-24, shesha)", () => {
  interface Business {
    id: string;
    config: string;
    guidance: string[];
    contact: string[];
    followUp: string;
  }
  const pawsConfig = fixture("config", "paws-and-co-follow-ups").body as ConfigBody;
  const PAWS: Business = {
    id: "paws-and-co",
    config: "paws-and-co-follow-ups",
    guidance: pawsConfig.guidance,
    contact: pawsConfig.texts[0].contact_fallback,
    followUp: pawsConfig.texts[0].handoff_follow_up!,
  };
  const CLINIC: Business = {
    id: SUNRISE,
    config: "sunrise-clinic-follow-ups",
    guidance: GUIDANCE,
    contact: CONTACT,
    followUp: HANDOFF_FOLLOW_UP,
  };

  /** A message case as the API sends it to `business`: with that business's own guidance. */
  function turnFor(name: string, business: Business, guidance: string[] = business.guidance) {
    const reply = fixture("messages", name);
    (reply.body as { turn: { guidance: string[] } }).turn.guidance = [...guidance];
    return reply;
  }

  function open(business: Business, api: FakeApi): Promise<Harness> {
    return openWidget({ api, businessId: business.id, config: fixture("config", business.config) });
  }

  function polls(h: Harness): number {
    return h.api.count(ROUTE.status(HANDOFF_ID));
  }

  /** No follow-up (the business has one: never vacuous), the guidance first, the contact. */
  function assertNotCleared(h: Harness, business: Business, guidance: string[] = business.guidance): void {
    const notice = h.view().notice;
    assert.equal(notice?.kind, "handoff");
    assert.equal(notice?.followUp, null);
    assert.ok(!shownTexts(h.view()).includes(business.followUp), "no 'a person will reply' text");
    assertGuidanceFirst(h, guidance);
    assertContact(h, business.contact);
  }

  for (const { label, business, message, urgent, status } of [
    {
      label: "not_covered_language at paws-and-co (a Telugu message; high)",
      business: PAWS,
      message: "handoff-not-covered",
      urgent: false,
      status: [en.notCoveredLanguage],
    },
    {
      label: "safety_floor_off at sunrise-clinic (a health business, the router off; normal)",
      business: CLINIC,
      message: "handoff-safety-floor-off",
      urgent: false,
      status: [],
    },
  ]) {
    test(`${label}: the guidance and the contact, no follow-up, no polling`, async () => {
      const api = withSession(new FakeApi(), business.id)
        .on(ROUTE.message, turnFor(message, business))
        .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
      const h = await open(business, api);
      await h.say("Hello");
      const notice = h.state().notice;
      assert.ok(notice?.kind === "handoff" && notice.queued, "the row was stored");
      assert.equal(notice.urgent, urgent, "not an urgent hand-off: the rule is the guidance");
      assertNotCleared(h, business);
      assertHeading(h, false); // not urgent: the conditional heading, as before
      assert.deepEqual(h.view().notice?.status.map((line) => line.text), status);
      await h.timers.advance(60_000);
      assert.equal(polls(h), 0, "no polling");
    });
  }

  for (const business of [PAWS, CLINIC]) {
    test(`a plain customer_asked hand-off at ${business.id} still gets the follow-up and polling (the button)`, async () => {
      const api = withSession(new FakeApi(), business.id)
        .on(ROUTE.handoff, fixture("handoffs", "created"))
        .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
      const h = await open(business, api);
      await h.widget.talkToPerson();
      assert.deepEqual(h.view().notice?.followUp, { text: business.followUp, lang: "en" });
      assertGuidanceFirst(h, business.guidance); // the cached copy, as in every hand-off (S16)
      await h.timers.advance(5_000);
      assert.equal(polls(h), 1);
    });

    test(`a plain customer_asked hand-off at ${business.id} still gets the follow-up and polling (a turn without guidance)`, async () => {
      const api = withSession(new FakeApi(), business.id)
        .on(ROUTE.message, fixture("messages", "handoff-normal-queued"))
        .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
      const h = await open(business, api);
      await h.say("I want to talk to someone");
      assert.deepEqual(h.view().notice?.followUp, { text: business.followUp, lang: "en" });
      await h.timers.advance(5_000);
      assert.equal(polls(h), 1);
    });
  }

  test("one line of guidance is enough", async () => {
    const line = [PAWS.guidance[0]];
    const api = withSession(new FakeApi(), PAWS.id)
      .on(ROUTE.message, turnFor("handoff-not-covered", PAWS, line))
      .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
    const h = await open(PAWS, api);
    await h.say("Hello");
    assertNotCleared(h, PAWS, line);
    await h.timers.advance(60_000);
    assert.equal(polls(h), 0, "no polling");
  });

  test("remembered for the session: after later turns, neither a hand-off turn without guidance nor 'Talk to a person' gets the follow-up or polling", async () => {
    const api = withSession()
      .on(
        ROUTE.message,
        turnFor("handoff-safety-floor-off", CLINIC),
        fixture("messages", "answer"),
        fixture("messages", "reply"),
        fixture("messages", "handoff-normal-queued"),
      )
      .on(ROUTE.handoff, fixture("handoffs", "created"))
      .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
    const h = await open(CLINIC, api);
    await h.say("Hello");
    await h.say("What are your opening hours?");
    assert.equal(h.state().transcript.at(-1)?.kind, "answer");
    await h.say("Thank you");
    assert.equal(h.state().transcript.at(-1)?.kind, "reply");
    await h.say("I want to talk to someone"); // customer_asked, normal, no guidance, stored
    assertNotCleared(h, CLINIC);
    await h.widget.talkToPerson(); // nothing in flight: settled, the row seen; still no follow-up
    assertNotCleared(h, CLINIC);
    await h.timers.advance(60_000);
    assert.equal(polls(h), 0, "no polling");
    assert.equal(h.api.count(ROUTE.session), 1, "one session throughout");
  });

  test("after a polled hand-off, one that carries guidance stops polling; a reply already in flight is dropped (C28 (a))", async () => {
    const poll = new Held();
    const api = withSession()
      .on(ROUTE.handoff, fixture("handoffs", "created"))
      .on(ROUTE.message, turnFor("handoff-safety-floor-off", CLINIC))
      .on(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"), poll)
      .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
    const h = await open(CLINIC, api);
    await h.widget.talkToPerson();
    assert.deepEqual(h.view().notice?.followUp, { text: HANDOFF_FOLLOW_UP, lang: "en" });
    await h.timers.advance(10_000); // two polls: "open", then one held in flight
    assert.equal(polls(h), 2);
    await h.say("Hello");
    poll.release(fixture("handoff-status", "replied"));
    await settle();
    assert.equal(h.state().polling, null);
    assert.ok(h.state().transcript.every((entry) => entry.kind !== "staff_reply"));
    assertNotCleared(h, CLINIC);
    await h.timers.advance(60_000);
    assert.equal(polls(h), 2, "polling stopped");
  });

  test("End chat, then a new session clears it: 'Talk to a person' gets the follow-up and polling again", async () => {
    const api = withSession(withSession(new FakeApi(), PAWS.id), PAWS.id)
      .on(ROUTE.message, turnFor("handoff-not-covered", PAWS))
      .on(ROUTE.end, fixture("end-session", "revoked"))
      .on(ROUTE.handoff, fixture("handoffs", "created"))
      .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
    const h = await open(PAWS, api);
    await h.say("Hello");
    assertNotCleared(h, PAWS);
    await h.widget.endChat();
    h.widget.startNew();
    await h.widget.talkToPerson();
    assert.equal(h.api.count(ROUTE.session), 2, "a new session");
    assert.deepEqual(h.view().notice?.followUp, { text: PAWS.followUp, lang: "en" });
    await h.timers.advance(5_000);
    assert.equal(polls(h), 1);
  });

  test("a 401, then a new session clears it too", async () => {
    const api = withSession(withSession())
      .on(
        ROUTE.message,
        turnFor("handoff-safety-floor-off", CLINIC),
        fixture("messages", "unauthorized"),
        fixture("messages", "answer"),
      )
      .on(ROUTE.handoff, fixture("handoffs", "created"))
      .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "open"));
    const h = await open(CLINIC, api);
    await h.say("Hello");
    await h.say("Are you there?");
    assert.equal(h.state().notice?.kind, "session_expired");
    await h.say("Are you there?");
    assert.equal(h.api.count(ROUTE.session), 2, "a new session");
    await h.widget.talkToPerson();
    assert.deepEqual(h.view().notice?.followUp, { text: HANDOFF_FOLLOW_UP, lang: "en" });
    await h.timers.advance(5_000);
    assert.equal(polls(h), 1);
  });
});

describe("the guidance's heading says when it applies (zara's review)", () => {
  for (const [label, reply] of [
    ["a network error", "network"],
    ["a 401", fixture("messages", "unauthorized")],
    ["a 422", fixture("messages", "invalid")],
    ["a 429", fixture("messages", "rate-limited")],
    ["a 5xx", fixture("messages", "server-error")],
    ["an unknown kind", fixture("messages", "unknown-kind")],
    ["no answer", fixture("messages", "answer-dont-know")],
    ["a normal hand-off", fixture("messages", "handoff-normal-queued")],
    ["a not_covered_language hand-off", fixture("messages", "handoff-not-covered")],
  ] as const) {
    test(`${label}: the guidance under 'If this is an emergency:'`, async () => {
      const h = await openWidget({ api: withSession().on(ROUTE.message, reply) });
      await h.say("When are you open?");
      assertGuidanceFirst(h);
      assertHeading(h, false);
    });
  }

  test("a client timeout and a failed session creation: the conditional heading", async () => {
    const h = await openWidget({ api: withSession().on(ROUTE.message, "hang") });
    h.widget.setDraft("Hello");
    void h.widget.send();
    await settle();
    await h.timers.advance(65_000);
    assert.equal(h.view().notice?.kind, "timeout");
    assertHeading(h, false);
    const refused = await openWidget({ api: new FakeApi().on(ROUTE.session, fixture("sessions", "limits-unavailable")) });
    await refused.say("Hello");
    assertHeading(refused, false);
  });

  test("an urgent hand-off: 'Safety information', and so is the guidance pinned after it", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "handoff-safety"), fixture("messages", "reply"), "network");
    const h = await openWidget({ api });
    await h.say("I feel faint");
    assertHeading(h, true);
    await h.say("Thank you");
    assert.equal(h.view().notice, null);
    assertHeading(h, true);
    await h.say("Are you there?");
    assert.equal(h.view().notice?.kind, "network_error");
    assertHeading(h, true);
  });
});

describe("End chat after an urgent hand-off (zara's review)", () => {
  test("the guidance stays; the transcript and the token go", async () => {
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "handoff-safety"))
      .on(ROUTE.end, fixture("end-session", "revoked"));
    const h = await openWidget({ api });
    await h.say("My dog ate rat poison");
    await h.widget.endChat();
    assert.equal(h.view().phase, "ended");
    assertGuidanceFirst(h);
    assertHeading(h, true);
    assert.deepEqual(h.state().transcript, []);
    assert.deepEqual(h.storage.keys(), []);
    h.widget.startNew();
    assertGuidanceFirst(h); // pinned for the rest of this view [proposal]
    assert.equal(h.state().urgentSeen, false, "a new chat gets a new session and a new row");
  });
});

describe("a configuration it cannot read still shows its guidance (zara's review)", () => {
  function config(change: (body: Record<string, unknown>) => void) {
    const item = fixture("config", "sunrise-clinic");
    change(item.body as Record<string, unknown>);
    return item;
  }

  test("an accent colour it cannot read is ignored: the chat opens with the default accent", async () => {
    const h = await openWidget({ config: config((body) => (body.accent_color = 15)) });
    assert.equal(h.view().phase, "chat");
    assert.equal(h.view().accent, DEFAULT_ACCENT);
  });

  for (const [label, change, lang] of [
    ["a quick reply that is not text", (body: Record<string, unknown>) => ((body.texts as { quick_replies: unknown[] }[])[0].quick_replies = [1]), "en"],
    ["a timeout of 0", (body: Record<string, unknown>) => (body.message_timeout_seconds = 0), "en"],
    ["guidance in Hindi (a future reviewed safety file)", (body: Record<string, unknown>) => (body.guidance_language = "hi"), "hi"],
  ] as const) {
    test(`${label}: unavailable, the guidance first, in its declared language`, async () => {
      const h = await openWidget({ config: config(change) });
      const view = h.view();
      assert.equal(view.phase, "unavailable");
      assert.deepEqual(view.guidance?.lines.map((line) => line.text), GUIDANCE);
      assert.equal(view.guidance?.lang, lang);
      assert.ok(view.guidance?.lines.every((line) => line.lang === lang));
      assert.equal(view.guidance?.heading.text, SAFETY.guidanceHeading);
      assert.equal(view.guidance?.heading.lang, "en");
    });
  }

  test("a config route that answers 404: unavailable, and no guidance to show", async () => {
    const h = await openWidget({ config: fixture("config", "not-found") });
    assert.equal(h.view().phase, "unavailable");
    assert.equal(h.view().guidance, null);
  });

  test("'Try again' keeps the guidance on screen while it loads, and the chat then opens", async () => {
    const h = await openWidget({ config: config((body) => (body.message_timeout_seconds = 0)) });
    assert.equal(h.view().phase, "unavailable");
    h.api.on(ROUTE.config(SUNRISE), fixture("config", "sunrise-clinic"));
    h.widget.retryConfig();
    assert.equal(h.view().phase, "loading");
    assert.deepEqual(guidanceOf(h), GUIDANCE);
    await settle();
    assert.equal(h.view().phase, "chat");
    assert.equal(h.view().guidance, null, "the greeting is not a failure");
  });
});

// ── Mutation probes (M8 PR 4 review): each kills a mutant the first suite let survive ─────────

describe("S8, S16: 'Talk to a person' answered every way (mutation review)", () => {
  for (const [label, reply, kind] of [
    ["a 5xx", { status: 500, model: "none", body: null }, "server_error"],
    ["a network error", "network", "network_error"],
    ["a client timeout", "hang", "timeout"],
    ["a 401", fixture("handoffs", "unauthorized"), "session_expired"],
  ] as const) {
    test(`answered by ${label}: guidance first, the contact, never a follow-up`, async () => {
      const h = await openWidget({
        api: withSession().on(ROUTE.handoff, reply),
        config: fixture("config", "sunrise-clinic-follow-ups"),
      });
      const pressed = h.widget.talkToPerson();
      await settle();
      await h.timers.advance(15_000); // BOOTSTRAP_TIMEOUT_MS, for the hang
      await pressed;
      assert.equal(h.view().notice?.kind, kind);
      assertGuidanceFirst(h);
      assertContact(h);
      assertNoPersonWillReply(h);
      assert.equal(h.view().talkToPerson.enabled, true);
    });
  }

  test("answered 429: guidance and a countdown, and Send stays available", async () => {
    // No hand-off 429 exists today (S17: the route has no limiter); the message 429's body has
    // the same shape, so it stands in for a future one.
    const h = await openWidget({ api: withSession().on(ROUTE.handoff, fixture("messages", "rate-limited")) });
    await h.widget.talkToPerson();
    assert.equal(h.view().notice?.kind, "rate_limited");
    assertGuidanceFirst(h);
    h.widget.setDraft("Hello");
    assert.equal(h.view().composer?.canSend, true);
  });

  test("in flight: the button is disabled and a second press sends nothing", async () => {
    const h = await openWidget({ api: withSession().on(ROUTE.handoff, "hang") });
    void h.widget.talkToPerson();
    await settle();
    assert.equal(h.view().talkToPerson.enabled, false);
    void h.widget.talkToPerson();
    await settle();
    assert.equal(h.api.count(ROUTE.handoff), 1);
  });

  test("a poll body for another hand-off is ignored, and polling goes on", async () => {
    const other = fixture("handoff-status", "replied");
    (other.body as { id: string }).id = "00000000-0000-4000-8000-0000000000ff";
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "handoff-normal-queued"))
      .on(ROUTE.status(HANDOFF_ID), other, fixture("handoff-status", "open"));
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    await h.say("I want to talk to someone");
    await h.timers.advance(5_000);
    assert.ok(h.view().transcript.every((entry) => entry.kind !== "staff_reply"));
    await h.timers.advance(5_000);
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 2, "still polling");
  });

  test("a hand-off turn's own guidance is shown, and stays pinned after it", async () => {
    const safety = fixture("messages", "handoff-safety");
    (safety.body as { turn: { guidance: string[] } }).turn.guidance = ["A line only this turn carries."];
    const h = await openWidget({ api: withSession().on(ROUTE.message, safety, fixture("messages", "reply")) });
    await h.say("Help");
    assertGuidanceFirst(h, ["A line only this turn carries."]);
    await h.say("Thank you");
    assertGuidanceFirst(h, ["A line only this turn carries."]);
  });

  test("a staff reply from a poll already in flight is not shown once the outcome is unknown (D11; C28 decided (a), 2026-11-24, shesha)", async () => {
    const poll = new Held();
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "handoff-normal-queued"), fixture("messages", "server-error"))
      .on(ROUTE.status(HANDOFF_ID), poll);
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    await h.say("I want to talk to someone");
    await h.timers.advance(5_000); // the poll goes out and waits
    await h.say("My chest hurts"); // a 500: the row may now be urgent
    poll.release(fixture("handoff-status", "replied"));
    await settle();
    assert.equal(h.state().polling, null);
    assert.ok(h.view().transcript.every((entry) => entry.kind !== "staff_reply"));
  });
});

describe("S16, D15: a 401 elsewhere while a message is in flight (mutation review)", () => {
  test("a poll's 401: session ended, 'may still be processed', the text back, nothing re-sent", async () => {
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "handoff-normal-queued"), "hang")
      .on(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "unauthorized"));
    const h = await openWidget({ api, config: fixture("config", "sunrise-clinic-follow-ups") });
    await h.say("I want to talk to someone");
    h.widget.setDraft("My chest hurts");
    void h.widget.send();
    await settle();
    await h.timers.advance(5_000); // the poll answers 401
    assert.equal(h.view().notice?.kind, "session_expired");
    assertGuidanceFirst(h);
    assert.equal(h.state().hasSession, false);
    assert.equal(h.state().draft, "My chest hurts");
    const last = h.view().transcript.filter((entry) => entry.kind === "customer").at(-1);
    assert.equal(last?.kind === "customer" ? last.status?.text : undefined, en.mayBeProcessed);
    assert.ok(shownTexts(h.view()).includes(en.mayStillBeProcessed));
    await h.timers.advance(10 * 60_000);
    assert.equal(h.api.count(ROUTE.message), 2, "nothing re-sent");
    assert.equal(h.api.count(ROUTE.session), 1, "no new session by itself");
    assert.equal(h.api.count(ROUTE.status(HANDOFF_ID)), 1, "polling stopped");
  });

  test("after a timeout, a later success clears 'may still be processed'; a 429 after it never shows it", async () => {
    const api = withSession().on(ROUTE.message, "hang", fixture("messages", "reply"), fixture("messages", "rate-limited"));
    const h = await openWidget({ api });
    h.widget.setDraft("Is the lab open?");
    void h.widget.send();
    await settle();
    await h.timers.advance(65_000);
    await h.widget.send(); // the customer's own retry of the restored text
    await settle();
    assert.equal(h.view().transcript.at(-1)?.kind, "reply");
    await h.say("And on Sunday?");
    assert.equal(h.view().notice?.kind, "rate_limited");
    assert.equal(h.view().composer?.hint, null);
    assert.ok(!shownTexts(h.view()).includes(en.mayStillBeProcessed));
  });
});

describe("one request at a time (mutation review)", () => {
  test("while End chat is in flight: Send, End chat and the card are disabled, and nothing else goes out", async () => {
    const api = withSession().on(ROUTE.message, fixture("messages", "confirmation-book")).on(ROUTE.end, "hang");
    const h = await openWidget({ api });
    await h.say("Book me in");
    h.widget.setDraft("One more");
    void h.widget.endChat();
    await settle();
    assert.equal(h.view().composer?.canSend, false);
    assert.equal(h.view().endChat.enabled, false);
    const entry = h.view().transcript.find((item) => item.kind === "confirm");
    assert.ok(entry?.kind === "confirm");
    assert.equal(entry.card.confirm?.enabled, false);
    assert.equal(entry.card.notNow?.enabled, false);
    await h.widget.send();
    await h.widget.confirm(ACTION);
    void h.widget.endChat();
    await settle();
    assert.equal(h.api.count(ROUTE.message), 1);
    assert.equal(h.api.count(ROUTE.end), 1);
    assert.equal(h.api.count(ROUTE.confirm(ACTION)), 0);
  });

  test("quick replies are disabled while sends are blocked (a 429 on session creation)", async () => {
    const h = await openWidget({ api: new FakeApi().on(ROUTE.session, fixture("sessions", "rate-limited")) });
    await h.widget.talkToPerson();
    assert.equal(h.view().notice?.kind, "rate_limited");
    const quick = h.view().greeting?.quickReplies ?? [];
    assert.ok(quick.length > 0);
    assert.ok(quick.every((reply) => !reply.enabled));
  });

  test("a session-creation 429 with a zero delay blocks nothing", async () => {
    const limited = fixture("sessions", "rate-limited");
    (limited.body as { retry_after_seconds: number }).retry_after_seconds = 0;
    limited.headers = { "Retry-After": "0" };
    const h = await openWidget({ api: new FakeApi().on(ROUTE.session, limited) });
    await h.say("Hello");
    assert.equal(h.view().notice?.kind, "rate_limited");
    assert.equal(h.view().composer?.canSend, true);
  });
});

class ThrowingStorage extends SpyStorage {
  getItem(): string | null {
    throw new DOMException("storage is blocked", "SecurityError");
  }
  setItem(): void {
    throw new DOMException("storage is full", "QuotaExceededError");
  }
  removeItem(): void {
    throw new DOMException("storage is blocked", "SecurityError");
  }
}

describe("S18: storage (mutation review)", () => {
  test("storage that throws degrades to memory: the chat still opens, answers and ends", async () => {
    const store = tokenStore(new ThrowingStorage());
    assert.equal(store.read(SUNRISE), null);
    assert.doesNotThrow(() => store.write(SUNRISE, TOKEN));
    assert.doesNotThrow(() => store.clear(SUNRISE));
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "reply"))
      .on(ROUTE.end, { status: 204, model: "none", body: null });
    const h = await openWidget({ api, storage: new ThrowingStorage() });
    assert.equal(h.view().phase, "chat");
    await h.say("Hello");
    assert.equal(h.view().transcript.at(-1)?.kind, "reply");
    await h.widget.endChat();
    assert.equal(h.view().phase, "ended");
  });

  test("a staff reply and a later failure are never written to storage", async () => {
    const storage = new SpyStorage();
    const api = withSession()
      .on(ROUTE.message, fixture("messages", "handoff-normal-queued"), "network")
      .on(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "replied"));
    const h = await openWidget({ api, storage, config: fixture("config", "sunrise-clinic-follow-ups") });
    await h.say("I want to talk to someone");
    await h.timers.advance(5_000);
    assert.equal(h.view().transcript.at(-1)?.kind, "staff_reply");
    await h.say("Thank you");
    assert.deepEqual(storage.writes, [[tokenKey(SUNRISE), TOKEN]]);
  });

  test("two businesses' widgets in one tab keep one key each", async () => {
    const storage = new SpyStorage();
    const sunrise = await openWidget({ storage, api: withSession().on(ROUTE.message, fixture("messages", "reply")) });
    await sunrise.say("Hello");
    const api = withSession(new FakeApi(), "paws-and-co").on(ROUTE.message, fixture("messages", "reply"));
    const paws = await openWidget({ businessId: "paws-and-co", storage, api });
    await paws.say("Hello");
    assert.equal(storage.keys().length, 2);
    assert.equal(storage.getItem(tokenKey(SUNRISE)), TOKEN);
  });
});

describe("S9: an answer needs outcome answered, text and sources (mutation review)", () => {
  type Answer = { outcome: string; answer: string | null };
  for (const [label, change] of [
    ["outcome handoff with text and sources", (answer: Answer) => void (answer.outcome = "handoff")],
    ["outcome dont_know with text and sources", (answer: Answer) => void (answer.outcome = "dont_know")],
    ["a blank answer with sources", (answer: Answer) => void (answer.answer = "  \n ")],
  ] as const) {
    test(`${label}: no answer; guidance and contact instead`, async () => {
      const body = fixture("messages", "answer");
      change((body.body as { turn: { answer: Answer } }).turn.answer);
      const h = await openWidget({ api: withSession().on(ROUTE.message, body) });
      await h.say("When are you open?");
      assert.ok(h.view().transcript.every((entry) => entry.kind !== "answer"));
      assert.equal(h.view().notice?.kind, "no_answer");
      assertGuidanceFirst(h);
      assertContact(h);
    });
  }
});

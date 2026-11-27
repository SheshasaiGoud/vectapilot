/**
 * The widget's HTML (`react-dom/server`): S9's XSS fixtures create no element and no attribute,
 * and S8/S16's guidance comes first, as `role="alert"` with `lang="en"`.
 */

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { SAFETY } from "../i18n/safety.ts";
import { WidgetView } from "../ui/WidgetView.tsx";
import type { WidgetActions } from "../ui/WidgetView.tsx";
import { FakeApi, ROUTE, fixture, openWidget, settle } from "./harness.ts";
import type { Harness } from "./harness.ts";

const NOOP: WidgetActions = {
  setDraft() {},
  compositionStart() {},
  compositionEnd() {},
  send() {},
  sendQuickReply() {},
  talkToPerson() {},
  confirm() {},
  notNow() {},
  endChat() {},
  startNew() {},
  retryConfig() {},
};

const HANDOFF_ID = "6f1c2a9e-0d4b-4c8e-9a51-2b7e3f4a5c61";

function html(h: Harness): string {
  return renderToStaticMarkup(createElement(WidgetView, { view: h.view(), actions: NOOP }));
}

/** No element or attribute came from the payloads, and they are visible as text. React escapes
 * `<`, `>`, `&` and quotes in text and attribute values, so every literal `<` is a real tag. */
function assertInert(markup: string): void {
  const tags = markup.match(/<[a-z][^>]*>/gi) ?? [];
  assert.ok(tags.length > 10, "the scan found the tags");
  for (const tag of tags) {
    assert.ok(!/^<(img|a|script|iframe|object|embed)[\s>]/i.test(tag), `no such element: ${tag}`);
    assert.ok(!/\son[a-z]+=/i.test(tag), `no inline event handler: ${tag}`);
    assert.ok(!/javascript:/i.test(tag), `no javascript: URL in an attribute: ${tag}`);
    assert.ok(!/\s(href|src|srcdoc|action|formaction)=/i.test(tag), `no URL attribute: ${tag}`);
  }
  assert.ok(markup.includes("&lt;img src=x onerror=alert(1)&gt;"), "the payload shows as text");
}

async function xssWidget(...messages: string[]): Promise<Harness> {
  const api = new FakeApi()
    .on(ROUTE.session, fixture("sessions", "created"))
    .on(ROUTE.message, ...messages.map((name) => fixture("messages", name)))
    .always(ROUTE.status(HANDOFF_ID), fixture("handoff-status", "xss-reply"));
  return openWidget({ api, config: fixture("config", "xss") });
}

describe("S9: untrusted text stays text", () => {
  test("the XSS configuration: name, greeting, quick replies", async () => {
    const h = await openWidget({ config: fixture("config", "xss") });
    const markup = html(h);
    assertInert(markup);
    assert.ok(markup.includes("javascript:alert(1)"), "the greeting is shown, as text");
  });

  for (const name of ["xss-answer", "xss-reply", "xss-confirmation", "xss-handoff"]) {
    test(`${name}: no element or attribute is created`, async () => {
      const h = await xssWidget(name);
      await h.say("<img src=x onerror=alert(1)> javascript:alert(1)");
      assertInert(html(h));
    });
  }

  test("an XSS contact fallback and network error state", async () => {
    const api = new FakeApi().on(ROUTE.session, "network");
    const h = await openWidget({ api, config: fixture("config", "xss") });
    await h.say("hello");
    assertInert(html(h));
  });

  test("an XSS staff reply", async () => {
    const h = await xssWidget("handoff-normal-queued");
    await h.say("hello");
    await h.timers.advance(5_000);
    const markup = html(h);
    assert.ok(h.view().transcript.some((entry) => entry.kind === "staff_reply"));
    assertInert(markup);
  });

  test("a citation's excerpt keeps its language and stays text", async () => {
    const h = await xssWidget("xss-answer");
    await h.say("hello");
    const markup = html(h);
    assert.match(markup, /<details[^>]*><summary[^>]*><span lang="en"[^>]*>&lt;img src=x/);
  });
});

describe("S8, S16: the guidance first, role=alert, lang=en, never animated", () => {
  test("a hand-off: guidance before the status, the follow-up and the contact", async () => {
    const api = new FakeApi()
      .on(ROUTE.session, fixture("sessions", "created"))
      .on(ROUTE.message, fixture("messages", "handoff-safety-not-stored"));
    const h = await openWidget({ api });
    await h.say("help");
    const markup = html(h);
    const alert = markup.indexOf('<div id="vp-guidance" role="alert" lang="en"');
    assert.ok(alert > 0);
    assert.ok(alert < markup.indexOf('role="status"'));
    assert.ok(alert < markup.indexOf("Other ways to reach"));
    const lines = (fixture("config", "sunrise-clinic").body as { guidance: string[] }).guidance;
    let at = alert;
    for (const line of lines) {
      const next = markup.indexOf(line, at);
      assert.ok(next > at, `in order: ${line}`);
      at = next;
    }
    const block = markup.slice(alert, markup.indexOf("</div>", alert));
    assert.ok(!/animate|transition|vp-dot/.test(block), "no animation class on the guidance");
  });

  test("a Hindi UI: chrome in Hindi, safety text and guidance in English, labelled", async () => {
    const api = new FakeApi().on(ROUTE.session, fixture("sessions", "limits-unavailable"));
    const h = await openWidget({ api, lang: "hi" });
    await h.say("नमस्ते");
    const markup = html(h);
    assert.match(markup, /^<div lang="hi"/);
    assert.ok(markup.includes('<p lang="en" class="text-sm text-gray-700">You are chatting with an AI assistant, not a person.</p>'));
    assert.ok(markup.includes('lang="en"') && markup.includes(">Talk to a person</button>"));
    assert.ok(markup.includes("सुरक्षा संदेश अंग्रेज़ी में दिखाए जाते हैं।"), "the safety-in-English label");
    assert.ok(markup.includes("इस अनुवाद की अभी किसी मूल भाषी ने समीक्षा नहीं की है।"), "unreviewed");
    assert.ok(markup.includes('<div id="vp-guidance" role="alert" lang="en"'));
  });

  test("the status region is always present, so a change is announced", async () => {
    const h = await openWidget();
    assert.ok(html(h).includes('<div role="status"'));
  });

  test("the hooks WidgetRoot moves focus to exist (WCAG 2.4.3)", async () => {
    const api = new FakeApi()
      .on(ROUTE.session, fixture("sessions", "created"))
      .on(ROUTE.message, fixture("messages", "confirmation-book"))
      .on(ROUTE.end, { status: 204, model: "none", body: null });
    const h = await openWidget({ api });
    await h.say("Book me in");
    const markup = html(h);
    assert.match(markup, /<section id="vp-cardbox-3d6f0a52-8b1e-4f7a-9c2d-5e8b1a7f4c30" tabindex="-1" data-vp-focus-return=""/);
    assert.ok(markup.includes('<textarea id="vp-message"'));
    await h.widget.endChat();
    assert.ok(html(h).includes('<button id="vp-start-new"'));
  });

  test("the transcript is a labelled log", async () => {
    const h = await openWidget();
    assert.ok(html(h).includes('<div role="log" aria-label="Conversation">'));
  });
});

describe("the guidance in every phase, its heading by state (zara's review)", () => {
  const GUIDANCE = (fixture("config", "sunrise-clinic").body as { guidance: string[] }).guidance;
  const ALERT = /<div id="vp-guidance" role="alert" lang="([a-z-]+)"[^>]*><h2 lang="en"[^>]*>([^<]*)<\/h2>/;

  test("a network error: the conditional heading", async () => {
    const api = new FakeApi().on(ROUTE.session, "network");
    const h = await openWidget({ api });
    await h.say("hello");
    const [, lang, heading] = ALERT.exec(html(h)) ?? [];
    assert.equal(lang, "en");
    assert.equal(heading, SAFETY.guidanceHeading);
  });

  test("an urgent hand-off, then End chat: still there, first, before the ended message", async () => {
    const api = new FakeApi()
      .on(ROUTE.session, fixture("sessions", "created"))
      .on(ROUTE.message, fixture("messages", "handoff-safety"))
      .on(ROUTE.end, fixture("end-session", "revoked"));
    const h = await openWidget({ api });
    await h.say("help");
    assert.equal(ALERT.exec(html(h))?.[2], SAFETY.guidanceHeadingUrgent);
    await h.widget.endChat();
    const markup = html(h);
    const alert = markup.search(ALERT);
    assert.ok(alert > 0, "the guidance is in the ended view");
    assert.ok(alert < markup.indexOf("Chat ended."));
    assert.ok(alert < markup.indexOf('id="vp-start-new"'));
    for (const line of GUIDANCE) assert.ok(markup.includes(line), line);
    const block = markup.slice(alert, markup.indexOf("</div>", alert));
    assert.ok(!/animate|transition|vp-dot/.test(block), "no animation class on the guidance");
  });

  test("a configuration it cannot read: the guidance, first, in its declared language", async () => {
    const config = fixture("config", "sunrise-clinic");
    Object.assign(config.body as object, { guidance_language: "hi" });
    const h = await openWidget({ config });
    const markup = html(h);
    const [, lang, heading] = ALERT.exec(markup) ?? [];
    assert.equal(lang, "hi");
    assert.equal(heading, SAFETY.guidanceHeading);
    assert.ok(markup.search(ALERT) < markup.indexOf("The chat is not available right now."));
  });
});

describe("controls and lines the markup must carry (leo's and the mutation review)", () => {
  test("Send is a disabled button in the HTML while a message is in flight", async () => {
    const h = await openWidget({ api: new FakeApi().on(ROUTE.session, fixture("sessions", "created")).on(ROUTE.message, "hang") });
    h.widget.setDraft("Hello");
    void h.widget.send();
    await settle();
    assert.match(html(h), /<button type="submit"[^>]*\sdisabled=""/);
  });

  test("a card's 429 countdown is shown but hidden from screen readers; its status line is not", async () => {
    const action = "3d6f0a52-8b1e-4f7a-9c2d-5e8b1a7f4c30";
    const api = new FakeApi()
      .on(ROUTE.session, fixture("sessions", "created"))
      .on(ROUTE.message, fixture("messages", "confirmation-book"))
      .on(ROUTE.confirm(action), fixture("confirm", "rate-limited"));
    const h = await openWidget({ api });
    await h.say("Book me in");
    const pending = h.widget.confirm(action);
    await settle();
    await pending;
    const markup = html(h);
    assert.match(markup, /<p aria-hidden="true" lang="en"[^>]*>Try again in 9 s\.<\/p>/);
    assert.match(markup, new RegExp(`<p lang="en"[^>]*>${SAFETY.cardRateLimited.replace(".", "\\.")}</p>`));
  });

  test("End chat while a confirm's outcome is unknown: the ended view says so, in English", async () => {
    const action = "3d6f0a52-8b1e-4f7a-9c2d-5e8b1a7f4c30";
    const api = new FakeApi()
      .on(ROUTE.session, fixture("sessions", "created"))
      .on(ROUTE.message, fixture("messages", "confirmation-book"))
      .on(ROUTE.confirm(action), fixture("confirm", "outcome-unknown"), fixture("confirm", "outcome-unknown"))
      .on(ROUTE.end, fixture("end-session", "revoked"));
    const h = await openWidget({ api });
    await h.say("Book me in");
    const pending = h.widget.confirm(action);
    await settle();
    await pending;
    await h.timers.advance(2_000);
    await h.widget.endChat();
    assert.ok(html(h).includes(`<p lang="en" class="font-medium">${SAFETY.cardUnknownHelp}</p>`));
  });
});

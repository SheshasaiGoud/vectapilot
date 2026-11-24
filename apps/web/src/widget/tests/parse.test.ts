/** Reading the API's bodies defensively (S9): unknown kinds, malformed bodies, planted ids. */

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import {
  namesHandoff,
  parseConfig,
  parseGuidance,
  parseHandoffRef,
  parseMessage,
  parseSession,
  parseTurn,
} from "../api/parse.ts";
import { parseRetryAfter } from "../api/client.ts";
import { allCases, fixture } from "./harness.ts";

const TRACE = "0".repeat(31) + "1";

describe("turns", () => {
  test("every message fixture with a known kind parses to that kind", () => {
    for (const [name, item] of Object.entries(allCases("messages"))) {
      if (item.status !== 200 || name === "unknown-kind" || name === "malformed") continue;
      const body = item.body as { turn: { kind: string } };
      assert.equal(parseMessage(item.body).turn.kind, body.turn.kind, name);
    }
  });

  for (const kind of ["stream", "answer_v2", "", "REPLY", "__proto__", "constructor"]) {
    test(`an unknown kind (${JSON.stringify(kind)}) is "unknown"`, () => {
      assert.deepEqual(parseTurn({ kind, trace_id: TRACE, text: "hello" }), { kind: "unknown" });
    });
  }

  test("a known kind with a missing or wrong field is unknown, never half-used", () => {
    assert.equal(parseTurn({ kind: "reply", trace_id: TRACE }).kind, "unknown");
    assert.equal(parseTurn({ kind: "reply", trace_id: TRACE, text: 7 }).kind, "unknown");
    assert.equal(parseTurn({ kind: "handoff", trace_id: TRACE, reason: "safety", priority: "panic" }).kind, "unknown");
    assert.equal(parseTurn({ kind: "handoff", trace_id: TRACE, reason: "safety", priority: "urgent", guidance: "one line" }).kind, "unknown");
    assert.equal(parseTurn({ kind: "answer", trace_id: TRACE, answer: { outcome: "answered", answer: "x", sources: [{}] } }).kind, "unknown");
    assert.equal(parseTurn({ kind: "answer", trace_id: TRACE, answer: { outcome: "guess", answer: "x", sources: [] } }).kind, "unknown");
    assert.equal(parseTurn(null).kind, "unknown");
    assert.equal(parseTurn([]).kind, "unknown");
  });

  test("a confirmation whose action id is not a UUID is unknown (it would go into a URL)", () => {
    const body = structuredClone(fixture("messages", "confirmation-book").body) as {
      turn: Record<string, unknown>;
    };
    body.turn.action_id = "../../staff/handoffs";
    assert.equal(parseMessage(body).turn.kind, "unknown");
  });

  test("a body that is not an object is an unknown turn with no hand-off", () => {
    assert.deepEqual(parseMessage("oops"), { turn: { kind: "unknown" }, handoff: null });
    assert.deepEqual(parseMessage(undefined), { turn: { kind: "unknown" }, handoff: null });
  });

  test("whether a body names a hand-off row, readable or not (zara's review)", () => {
    assert.equal(namesHandoff(fixture("messages", "handoff-safety").body), true);
    assert.equal(namesHandoff({ turn: { kind: "stream" }, handoff: { id: "x", queued: "yes" } }), true);
    assert.equal(namesHandoff(fixture("messages", "reply").body), false);
    assert.equal(namesHandoff({ turn: {} }), false);
    assert.equal(namesHandoff("not json"), false);
    assert.equal(namesHandoff(undefined), false);
  });

  test("a hand-off ref it cannot read counts as not stored", () => {
    assert.equal(parseHandoffRef({ id: "not-a-uuid", queued: true }), null);
    assert.equal(parseHandoffRef({ id: "6f1c2a9e-0d4b-4c8e-9a51-2b7e3f4a5c61", queued: "yes" }), null);
    const body = { turn: { kind: "handoff", trace_id: TRACE, reason: "safety", priority: "urgent", guidance: [] }, handoff: { id: 1 } };
    assert.equal(parseMessage(body).handoff, null);
  });
});

describe("configuration and session", () => {
  test("the config of another business is refused", () => {
    assert.equal(parseConfig(fixture("config", "sunrise-clinic").body, "paws-and-co"), null);
  });

  test("a config whose languages have no texts is refused", () => {
    const body = structuredClone(fixture("config", "sunrise-clinic").body) as { languages: string[] };
    body.languages.push("te");
    assert.equal(parseConfig(body, "sunrise-clinic"), null);
  });

  test("an accent colour it cannot read is ignored, never a reason to refuse the config", () => {
    for (const accent of [15, true, { r: 0 }, ["#000000"]]) {
      const body = structuredClone(fixture("config", "sunrise-clinic").body) as Record<string, unknown>;
      body.accent_color = accent;
      assert.equal(parseConfig(body, "sunrise-clinic")?.accent_color, null, JSON.stringify(accent));
    }
  });

  test("the guidance alone: read even when the rest of the config is not (zara's review)", () => {
    const body = structuredClone(fixture("config", "sunrise-clinic").body) as Record<string, unknown>;
    const lines = body.guidance as string[];
    (body.texts as { quick_replies: unknown[] }[])[0].quick_replies = [1];
    assert.equal(parseConfig(body, "sunrise-clinic"), null, "the rest fails closed");
    assert.deepEqual(parseGuidance(body, "sunrise-clinic"), { lines, lang: "en" });
    assert.deepEqual(parseGuidance({ ...body, guidance_language: "hi" }, "sunrise-clinic"), { lines, lang: "hi" });
    assert.equal(parseGuidance(body, "paws-and-co"), null, "never another business's");
    for (const bad of [{ guidance: [] }, { guidance: "Call 112" }, { guidance: [1] }, { guidance: [""] },
      { guidance_language: 'en" onfocus="x' }, { guidance_language: null }]) {
      assert.equal(parseGuidance({ ...body, ...bad }, "sunrise-clinic"), null, JSON.stringify(bad));
    }
  });

  test("PR 1's config has no timeouts: parsed as null", () => {
    const config = parseConfig(fixture("config", "sunrise-clinic").body, "sunrise-clinic");
    assert.equal(config?.message_timeout_seconds, null);
  });

  test("a session token naming another business is refused", () => {
    const body = structuredClone(fixture("sessions", "created").body) as { token: string };
    assert.ok(parseSession(body, "sunrise-clinic"));
    assert.equal(parseSession(body, "paws-and-co"), null);
  });

  test("Retry-After in seconds only", () => {
    assert.equal(parseRetryAfter("12"), 12);
    assert.equal(parseRetryAfter(" 3 "), 3);
    assert.equal(parseRetryAfter("Wed, 21 Oct 2026 07:28:00 GMT"), null);
    assert.equal(parseRetryAfter("-1"), null);
    assert.equal(parseRetryAfter(null), null);
  });
});

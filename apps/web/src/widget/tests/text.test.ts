/** S16: the length rule in the server's unit, code points (`MAX_QUESTION_CHARS` 2000). */

import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { MAX_MESSAGE_CODE_POINTS, checkLength, codePointCount } from "../core/text.ts";
import { openWidget } from "./harness.ts";

// "తెలుగు" is 3 graphemes a reader sees but 6 code points: త ె ల ు గ ు.
const TELUGU_WORD = "తెలుగు";
const telugu = (codePoints: number): string => {
  const whole = TELUGU_WORD.repeat(Math.floor(codePoints / 6));
  return whole + Array.from(TELUGU_WORD).slice(0, codePoints % 6).join("");
};

function graphemes(text: string): number {
  return [...new Intl.Segmenter("te", { granularity: "grapheme" }).segment(text)].length;
}

describe("message length in code points", () => {
  test("the bound mirrors the API's MAX_QUESTION_CHARS", () => {
    assert.equal(MAX_MESSAGE_CODE_POINTS, 2000);
  });

  test("a Telugu text of exactly 2,000 code points is accepted, 2,001 is not", () => {
    const atBound = telugu(2000);
    assert.equal(codePointCount(atBound), 2000);
    assert.deepEqual(checkLength(atBound), { count: 2000, max: 2000, over: 0, ok: true });
    const over = `${atBound}ు`;
    assert.deepEqual(checkLength(over), { count: 2001, max: 2000, over: 1, ok: false });
  });

  test("the Telugu text looks far shorter than it is: graphemes are not the unit", () => {
    const atBound = telugu(2000);
    assert.ok(graphemes(atBound) < 1100, `graphemes: ${graphemes(atBound)}`);
    assert.equal(codePointCount(atBound), 2000);
  });

  test("UTF-16 units are not the unit either: an emoji is one code point, two units", () => {
    const text = "🙂".repeat(2000);
    assert.equal(text.length, 4000);
    assert.equal(checkLength(text).ok, true);
  });

  test("blank text is never sendable", () => {
    assert.equal(checkLength("").ok, false);
    assert.equal(checkLength(" \n\t ").ok, false);
  });

  test("the counter and the over-limit line count code points, not UTF-16 units (mutation review)", async () => {
    const h = await openWidget();
    h.widget.setDraft("🙂".repeat(3));
    assert.equal(h.view().composer?.counter.text, "3 of 2000 characters");
    h.widget.setDraft("🙂".repeat(1500));
    assert.equal(h.view().composer?.overLimit, null, "1,500 code points (3,000 units) are within the limit");
    assert.equal(h.view().composer?.canSend, true);
    h.widget.setDraft("🙂".repeat(2001));
    assert.equal(h.view().composer?.overLimit?.text, "Too long to send: remove 1 characters.");
  });

  test("the widget refuses to send 2,001 code points and shows how many to remove", async () => {
    const h = await openWidget({ businessId: "paws-and-co", lang: "te" });
    h.widget.setDraft(telugu(2001));
    assert.equal(h.widget.canSend(), false);
    const composer = h.view().composer;
    assert.ok(composer?.overLimit?.text.includes("1"));
    assert.equal(composer?.canSend, false);
    await h.widget.send();
    assert.equal(h.api.calls.length, 1, "only the config request: nothing was sent");
    h.widget.setDraft(telugu(2000));
    assert.equal(h.view().composer?.overLimit, null);
    assert.equal(h.view().composer?.canSend, true);
  });
});

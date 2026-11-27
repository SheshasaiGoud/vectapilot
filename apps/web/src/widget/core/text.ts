/**
 * The message length rule, in the server's unit (S16): code points, as Python's `len()` counts
 * them, so the widget refuses exactly what the API would refuse (`MAX_QUESTION_CHARS` 2000,
 * `apps/api/src/vectapilot_api/retrieval/types.py:40`, counted before stripping).
 *
 * Not graphemes: a Telugu or Hindi letter with its vowel signs is one grapheme the reader sees but
 * two or three code points, so such a text can look shorter than it is. Not UTF-16 units either
 * (`text.length`, and a textarea's `maxlength`): an emoji is one code point but two units.
 */

export const MAX_MESSAGE_CODE_POINTS = 2000;

export function codePointCount(text: string): number {
  return Array.from(text).length;
}

/** No text to send: empty or only whitespace (the API strips, then refuses an empty message). */
export function isBlank(text: string): boolean {
  return text.trim() === "";
}

export interface LengthCheck {
  count: number;
  max: number;
  over: number;
  ok: boolean;
}

export function checkLength(text: string): LengthCheck {
  const count = codePointCount(text);
  const over = Math.max(0, count - MAX_MESSAGE_CODE_POINTS);
  return { count, max: MAX_MESSAGE_CODE_POINTS, over, ok: !isBlank(text) && over === 0 };
}

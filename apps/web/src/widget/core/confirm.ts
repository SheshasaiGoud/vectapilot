/**
 * The confirmation card's answer to each confirm response (leo's table, M8 PR 4; ADR 0016 W2,
 * W5, W6, W9). Pure: the controller sends, this decides.
 *
 * | Response                                   | Card                         | Then                   |
 * |--------------------------------------------|------------------------------|------------------------|
 * | 200 `executed`                             | the server's fixed sentence  | done                   |
 * | 409 `failed`                               | the server's fixed sentence  | done                   |
 * | 404 `action_not_found`                     | "Expired. Nothing was done." | done                   |
 * | 503 `confirm_failed` / `limits_unavailable`| "Not done yet"               | retry, same token      |
 * | 503 `outcome_unknown`, timeout, network    | "We are checking"            | one automatic retry,   |
 * |   (and any other answer it cannot read)    |   never "nothing happened"   | then manual, same token|
 * | 429                                        | a countdown                  | same token             |
 * | 401, or any other 404 (`widget_not_found`: | session ended; after an      | guidance (S16)         |
 * |   the widget was disabled)                 |   unknown outcome, no claim  |                        |
 *
 * **Once an attempt's outcome was unknown, no later answer may say "not done"** (leo's review):
 * a `confirm_failed` or `limits_unavailable` only says *this* attempt changed nothing; it tells
 * nothing about the earlier attempt. They keep "We could not check yet" (`check_manual`), the
 * same token and a manual retry.
 *
 * **A 404 other than `action_not_found` is a 401** (zara's PR 2 review): `widget_not_found` means
 * the session's widget was disabled, and every later request on that session gets it too, so the
 * session has ended for this page. The card says so and makes no claim that nothing was done
 * (after an unknown outcome it adds "We could not check whether this was done"); the guidance
 * shows (S16) and the next send starts a new session.
 *
 * Only `action_not_found` is "expired": this session holds this token, and the confirm service
 * returns an executed action's stored result whatever its expiry (`actions/confirm.py`: lock,
 * token, stored result, then expiry), so that 404 means it was not done, even after an unknown
 * outcome.
 */

import type { HttpResult } from "../api/client.ts";
import { parseConfirm, parseError } from "../api/parse.ts";
import type { Card } from "./state.ts";

export type ConfirmEffect = "none" | "auto_retry" | "session_expired";

/** [proposal] When a 429 names no delay. Configuration, not a measurement. */
export const DEFAULT_RETRY_AFTER_S = 30;

/** 503s that say this attempt ran nothing (the service never ran, or changed nothing). */
const NOT_DONE = new Set(["confirm_failed", "limits_unavailable"]);

/** This attempt ran nothing; after an earlier unknown outcome that proves nothing either way. */
function notDone(card: Card): { card: Card; effect: ConfirmEffect } {
  return card.unknownOutcome
    ? { card: { ...card, status: "check_manual" }, effect: "none" }
    : { card: { ...card, status: "not_done" }, effect: "none" };
}

function unknown(card: Card): { card: Card; effect: ConfirmEffect } {
  if (!card.autoRetried) {
    return {
      card: { ...card, status: "checking", unknownOutcome: true, autoRetried: true },
      effect: "auto_retry",
    };
  }
  return { card: { ...card, status: "check_manual", unknownOutcome: true }, effect: "none" };
}

export function applyConfirmResult(
  card: Card,
  result: HttpResult,
  now: number,
): { card: Card; effect: ConfirmEffect } {
  if (result.kind === "aborted") return { card, effect: "none" };
  if (result.kind !== "response") return unknown(card);
  const { status, body } = result;
  if (status === 200 || status === 409) {
    const parsed = parseConfirm(body, card.actionId);
    const want = status === 200 ? "executed" : "failed";
    if (parsed === null || parsed.status !== want) return unknown(card);
    return {
      card: { ...card, status: want, serverMessage: parsed.message, retryAt: null },
      effect: "none",
    };
  }
  if (status === 404 && parseError(body).error === "action_not_found") {
    return { card: { ...card, status: "expired" }, effect: "none" };
  }
  if (status === 401 || status === 404) {
    // Any other 404 (`widget_not_found`, or a body it cannot read) as a 401 (zara's PR 2 review).
    return { card: { ...card, status: "session_ended" }, effect: "session_expired" };
  }
  if (status === 429) {
    const seconds =
      parseError(body).retry_after_seconds ?? result.retryAfterSeconds ?? DEFAULT_RETRY_AFTER_S;
    return {
      card: { ...card, status: "rate_limited", retryAt: now + seconds * 1000 },
      effect: "none",
    };
  }
  if (status === 503 && NOT_DONE.has(parseError(body).error ?? "")) return notDone(card);
  return unknown(card); // 503 `outcome_unknown`, a 5xx, or anything it cannot read
}

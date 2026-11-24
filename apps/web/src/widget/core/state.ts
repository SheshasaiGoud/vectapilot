/**
 * The widget's state (M8 PR 4). Plain data, replaced (never mutated) on every change, so React
 * reads it with `useSyncExternalStore` and tests compare snapshots.
 *
 * Never in here (S18): the session token and the confirm tokens. The controller keeps them in
 * private fields; a test serialises the state and the view and finds neither.
 */

import type { Citation, WidgetConfig } from "../api/types.ts";
import { checkLength } from "./text.ts";

export type ConfigStatus = "booting" | "loading" | "ready" | "unavailable";

/** A customer message's fate: `unknown` after a timeout, a network error or a 5xx (D15). */
export type CustomerStatus = "sending" | "sent" | "not_sent" | "unknown";

export type Entry =
  | { id: number; kind: "customer"; text: string; status: CustomerStatus }
  | { id: number; kind: "answer"; text: string; sources: Citation[] }
  | { id: number; kind: "reply"; text: string }
  | { id: number; kind: "confirm"; actionId: string }
  | { id: number; kind: "not_answered" }
  | { id: number; kind: "no_answer" }
  | { id: number; kind: "staff_reply"; text: string };

/**
 * A confirmation card's state (leo's table, PR 4). Terminal: `executed`, `failed`, `expired`,
 * `session_ended`, `dismissed`, `dropped` (the confirm token is then gone from memory).
 * `checking`: an unknown outcome, the one automatic retry pending or running;
 * `check_manual`: still unknown, a manual retry with the same token until expiry.
 */
export type CardStatus =
  | "ready"
  | "in_flight"
  | "executed"
  | "failed"
  | "expired"
  | "not_done"
  | "checking"
  | "check_manual"
  | "rate_limited"
  | "session_ended"
  | "dismissed"
  | "dropped";

export const TERMINAL_CARD: ReadonlySet<CardStatus> = new Set([
  "executed",
  "failed",
  "expired",
  "session_ended",
  "dismissed",
  "dropped",
]);

/** Statuses in which the customer may press Confirm (or Try again) and Not now. */
export const ACTIONABLE_CARD: ReadonlySet<CardStatus> = new Set([
  "ready",
  "not_done",
  "check_manual",
  "rate_limited",
]);

export interface Card {
  actionId: string;
  tool: string;
  /** English, from the tool's fixed template (W3); may hold line breaks; shown as text. */
  summary: string;
  expiresAt: string;
  status: CardStatus;
  /** The server's fixed sentence for `executed` or `failed`. */
  serverMessage: string | null;
  /** Some attempt's outcome was unknown: no later text may claim nothing was done. */
  unknownOutcome: boolean;
  /** The one automatic retry after an unknown outcome has been used. */
  autoRetried: boolean;
  /** `rate_limited`: when the same token may be tried again (epoch ms). */
  retryAt: number | null;
}

/** Where a request failed: only a message send can leave "may still be processed" behind. */
export type During = "message" | "session" | "handoff";

export type Notice =
  | { kind: "working" }
  | { kind: "network_error"; during: During }
  | { kind: "timeout"; during: During }
  | { kind: "session_expired" }
  | { kind: "invalid_message" }
  | { kind: "rate_limited"; until: number; guidance: string[] | null }
  | { kind: "server_error" }
  | { kind: "session_unavailable" }
  | {
      kind: "handoff";
      origin: "turn" | "button";
      reason: string | null;
      urgent: boolean;
      /** The row exists (S8). */
      queued: boolean;
      /** Queued, not urgent, the row is known not to be urgent, and no turn hand-off on this
       * session carried guidance (Z4 (b)): a follow-up may show. */
      followUpAllowed: boolean;
      guidance: string[];
    }
  | { kind: "unknown_kind" }
  | { kind: "no_answer" }
  /** A confirm that did not end in a definite answer (unknown, not done, 429): the card says
   * which; the guidance and the contact show as for any other failure (S16, zara's review). */
  | { kind: "confirm_problem"; guidance: string[] | null };

export type NoticeKind = Notice["kind"];

/** The states that are not a success: each shows the guidance first (S16). */
export const NON_SUCCESS: ReadonlySet<NoticeKind> = new Set([
  "network_error",
  "timeout",
  "session_expired",
  "invalid_message",
  "rate_limited",
  "server_error",
  "session_unavailable",
  "handoff",
  "unknown_kind",
  "no_answer",
  "confirm_problem",
]);

export interface WidgetState {
  configStatus: ConfigStatus;
  businessId: string | null;
  /** The query's `lang`, before the configuration says which languages exist. */
  requestedLang: string | null;
  uiLanguage: string;
  config: WidgetConfig | null;
  hasSession: boolean;
  draft: string;
  /** An IME composition is open: Enter commits it and must never send (S16, PR 4). */
  composing: boolean;
  messageInFlight: boolean;
  handoffInFlight: boolean;
  endInFlight: boolean;
  transcript: Entry[];
  cards: Readonly<Record<string, Card>>;
  notice: Notice | null;
  /** After an urgent hand-off: its guidance stays pinned for the rest of this view [proposal],
   * "End chat" and a new chat included (public configuration, not customer data: zara). */
  urgentGuidance: string[] | null;
  /** An urgent hand-off on this chat's session: its row stays urgent (S17), and a confirmation
   * card offered after it arrives dropped (L5 decided (a), 2026-11-24, shesha; leo's and zara's
   * review). */
  urgentSeen: boolean;
  /** The configuration could not be read, but its guidance could: shown in the "unavailable"
   * state with its declared language (zara's review). */
  unavailableGuidance: { lines: string[]; lang: string } | null;
  /** "End chat" was used while a confirm's outcome was still unknown: the ended view says so
   * (leo's review), never that nothing was done. */
  endedWithUnknownAction: boolean;
  /** The last message's outcome is unknown: a manual retry says it may still be processed. */
  retryNote: boolean;
  /** A 429 on session creation: no send before this time (epoch ms). A message's 429 blocks
   * nothing: the server's limiter stays the authority, and a red-flag message skips it (M7 G1). */
  sendBlockedUntil: number | null;
  /** The clock the countdowns read, ticked once a second while one runs. */
  now: number;
  ended: boolean;
  /** The hand-off being polled for a staff reply (D7 (a)); never an urgent one. */
  polling: string | null;
}

export function initialState(now: number): WidgetState {
  return {
    configStatus: "booting",
    businessId: null,
    requestedLang: null,
    uiLanguage: "en",
    config: null,
    hasSession: false,
    draft: "",
    composing: false,
    messageInFlight: false,
    handoffInFlight: false,
    endInFlight: false,
    transcript: [],
    cards: {},
    notice: null,
    urgentGuidance: null,
    urgentSeen: false,
    unavailableGuidance: null,
    endedWithUnknownAction: false,
    retryNote: false,
    sendBlockedUntil: null,
    now,
    ended: false,
    polling: null,
  };
}

export function anyCardInFlight(state: WidgetState): boolean {
  return Object.values(state.cards).some(
    (card) => card.status === "in_flight" || card.status === "checking",
  );
}

/** Whether `text` would be sent now: the one rule the controller and the view share. */
export function canSend(state: WidgetState, text: string = state.draft): boolean {
  const blocked = state.sendBlockedUntil !== null && state.now < state.sendBlockedUntil;
  return (
    state.configStatus === "ready" &&
    !state.ended &&
    !state.messageInFlight &&
    !state.composing &&
    !state.endInFlight &&
    !blocked &&
    !anyCardInFlight(state) &&
    checkLength(text).ok
  );
}

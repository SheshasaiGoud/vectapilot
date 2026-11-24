/**
 * From state to what the widget shows (M8 PR 4): pure, so every rule is a unit test.
 *
 * Every visible text is a `Line` with its language (WCAG 3.1.2): chrome in the UI language (or
 * English where no dictionary exists), safety text and guidance in English (D9 (a), S8), business
 * text in its own configured language, model and customer text without a claimed language.
 *
 * The rules this function owns:
 * - **Guidance first in every state that is not a success** (S16), verbatim, `lang="en"`; a 429
 *   shows the body's guidance, else the cached copy; a hand-off the turn's, else the cached copy;
 *   after an urgent hand-off it stays pinned [proposal], "End chat" included. A business without
 *   guidance shows none. Its heading depends on the state (zara's review): an urgent hand-off's
 *   guidance is "Safety information"; anywhere else it is conditional, "If this is an
 *   emergency:", so a network blip never reads as an instruction to call emergency services.
 *   When the configuration cannot be read, its guidance alone is still shown.
 * - **Follow-up texts** (S8, D11): `handoff_follow_up` only when queued, not urgent, the row is
 *   known not to be urgent and no turn hand-off on the session carried guidance (Z4 (b): the
 *   controller's `followUpAllowed`); `urgent_follow_up` only when queued and urgent; otherwise
 *   nothing.
 *   The contact fallback in every state that is not a success, an urgent hand-off included
 *   (S8 as decided; zara's review: D11 forbids a reply prompt or an ETA, and the contact list is
 *   neither).
 * - **Answers only with sources** (S9): the controller never stores another kind of answer.
 * - **No secret** reaches the view: neither token is in the state it reads.
 */

import type { Citation, WidgetConfig, WidgetTexts } from "../api/types.ts";
import { chromeFor, format } from "../i18n/index.ts";
import type { Chrome } from "../i18n/index.ts";
import { SAFETY, SAFETY_LANG } from "../i18n/safety.ts";
import { ACTIONABLE_CARD, NON_SUCCESS, anyCardInFlight, canSend } from "./state.ts";
import type { Card, Entry, Notice, WidgetState } from "./state.ts";
import { checkLength } from "./text.ts";

/** A text and its language; `null`: inherit the widget's (customer and model text). */
export interface Line {
  text: string;
  lang: string | null;
}

export interface SourceView {
  key: string;
  title: Line;
  page: Line | null;
  excerpt: Line;
}

export interface CardView {
  actionId: string;
  heading: Line;
  englishLabel: Line;
  summary: Line;
  expires: Line | null;
  onlyButton: Line | null;
  status: Line | null;
  extra: Line | null;
  /** A 429's seconds, shown but never announced: the card sits in the transcript's live log, and
   * a number that changes every second would be read out every second (leo's review). */
  countdown: Line | null;
  busy: boolean;
  confirm: { label: Line; enabled: boolean } | null;
  notNow: { label: Line; enabled: boolean } | null;
}

export type EntryView =
  | { key: string; kind: "customer"; who: Line; text: Line; status: Line | null }
  | { key: string; kind: "answer"; who: Line; text: Line; sourcesHeading: Line; sources: SourceView[] }
  | { key: string; kind: "reply"; who: Line; provenance: Line; text: Line }
  | { key: string; kind: "confirm"; card: CardView }
  | { key: string; kind: "note"; text: Line }
  | { key: string; kind: "staff_reply"; who: Line; text: Line };

export interface NoticeView {
  kind: Notice["kind"];
  status: Line[];
  followUp: Line | null;
  countdown: Line | null;
  contact: { heading: Line; lines: Line[] } | null;
}

export interface ComposerView {
  value: string;
  label: Line;
  counter: Line;
  overLimit: Line | null;
  hint: Line | null;
  canSend: boolean;
  send: Line;
}

export interface WidgetView {
  phase: "loading" | "unavailable" | "chat" | "ended";
  uiLang: string;
  accent: string;
  heading: Line | null;
  aiDisclosure: Line;
  notForEmergencies: Line;
  safetyInEnglish: Line | null;
  translationNote: Line | null;
  talkToPerson: { label: Line; visible: boolean; enabled: boolean };
  loading: Line;
  unavailable: { message: Line; help: Line; retry: Line } | null;
  conversationLabel: Line;
  greeting: {
    text: Line;
    suggestionsLabel: Line;
    quickReplies: { line: Line; enabled: boolean }[];
  } | null;
  transcript: EntryView[];
  /** Shown first in the status area, `role="alert"`, never animated (S8, S15, S16). `urgent`: an
   * urgent hand-off's guidance (pinned after it), else the conditional heading. */
  guidance: { heading: Line; lines: Line[]; lang: string; urgent: boolean } | null;
  notice: NoticeView | null;
  composer: ComposerView | null;
  endChat: { label: Line; visible: boolean; enabled: boolean };
  /** `unknownAction`: "End chat" came while a confirm's outcome was unknown (leo's review). */
  ended: { message: Line; startNew: Line; unknownAction: Line | null } | null;
}

// ── Accent colour (re-checked here: configuration is data, S9) ──────────────────────────────────

export const DEFAULT_ACCENT = "#1e40af";
const HEX = /^#[0-9a-f]{6}$/;
const MIN_CONTRAST = 4.5; // WCAG 2.2 SC 1.4.3 against the white text written on it

function channel(value: number): number {
  const c = value / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

export function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => channel(parseInt(hex.slice(i, i + 2), 16)));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrastWithWhite(hex: string): number {
  return 1.05 / (luminance(hex) + 0.05);
}

export function safeAccent(color: string | null): string {
  return color !== null && HEX.test(color) && contrastWithWhite(color) >= MIN_CONTRAST
    ? color
    : DEFAULT_ACCENT;
}

// ── Helpers ─────────────────────────────────────────────────────────────────────────────────────

const safety = (text: string): Line => ({ text, lang: SAFETY_LANG });

function textsFor(config: WidgetConfig | null, language: string): WidgetTexts | null {
  return config?.texts.find((texts) => texts.language === language) ?? null;
}

function expiryTime(iso: string): string | null {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat("en", { hour: "numeric", minute: "2-digit" }).format(date);
}

function secondsLeft(until: number, now: number): number {
  return Math.max(0, Math.ceil((until - now) / 1000));
}

function guidanceLines(state: WidgetState): { lines: string[]; urgent: boolean } | null {
  const notice = state.notice;
  const cached = state.config?.guidance ?? [];
  let lines: string[] = [];
  if (notice !== null && NON_SUCCESS.has(notice.kind)) {
    if (notice.kind === "handoff") lines = notice.guidance.length ? notice.guidance : cached;
    else if (notice.kind === "rate_limited" || notice.kind === "confirm_problem") {
      lines = notice.guidance ?? cached;
    } else lines = cached;
  }
  if (lines.length === 0 && state.urgentGuidance !== null) lines = state.urgentGuidance;
  if (lines.length === 0) return null;
  const urgentNow = notice?.kind === "handoff" && notice.urgent;
  return { lines, urgent: urgentNow || state.urgentGuidance !== null };
}

function guidanceView(lines: string[], lang: string, urgent: boolean): NonNullable<WidgetView["guidance"]> {
  return {
    heading: safety(urgent ? SAFETY.guidanceHeadingUrgent : SAFETY.guidanceHeading),
    lines: lines.map((text) => ({ text, lang })),
    lang,
    urgent,
  };
}

function cardView(card: Card, state: WidgetState, chrome: Chrome): CardView {
  const otherBusy = state.messageInFlight || state.endInFlight || anyCardInFlight(state);
  const waiting = card.status === "rate_limited" && card.retryAt !== null && state.now < card.retryAt;
  const actionable = ACTIONABLE_CARD.has(card.status);
  const canConfirm = actionable && !waiting && !otherBusy && state.hasSession;
  const unknownHelp = card.unknownOutcome ? safety(SAFETY.cardUnknownHelp) : null;
  let status: Line | null = null;
  let extra: Line | null = null;
  let countdown: Line | null = null;
  switch (card.status) {
    case "ready":
      break;
    case "in_flight":
      status = safety(SAFETY.cardConfirming);
      break;
    case "executed":
    case "failed":
      status = card.serverMessage === null ? null : safety(card.serverMessage);
      break;
    case "expired":
      status = safety(SAFETY.cardExpired);
      break;
    case "not_done":
      status = safety(SAFETY.cardNotDone);
      extra = unknownHelp; // never "not done" alone after an unknown outcome
      break;
    case "checking":
      status = safety(SAFETY.cardChecking);
      break;
    case "check_manual":
      status = safety(SAFETY.cardStillChecking);
      break;
    case "rate_limited":
      // A fixed line, announced once; then "You can try again now.", announced once.
      status = safety(waiting ? SAFETY.cardRateLimited : SAFETY.cardCanTryAgain);
      countdown = waiting
        ? safety(format(SAFETY.cardRetryIn, { seconds: secondsLeft(card.retryAt ?? 0, state.now) }))
        : null;
      extra = unknownHelp; // a 429 says nothing about an earlier attempt
      break;
    case "session_ended":
      status = safety(SAFETY.cardSessionEnded);
      extra = unknownHelp; // never "nothing was done" after an unknown outcome
      break;
    case "dismissed":
      status = unknownHelp ?? safety(SAFETY.cardDismissed);
      break;
    case "dropped":
      status = unknownHelp ?? safety(SAFETY.cardDropped);
      break;
  }
  const live = actionable || card.status === "in_flight" || card.status === "checking";
  const expires = expiryTime(card.expiresAt);
  return {
    actionId: card.actionId,
    heading: safety(SAFETY.cardHeading),
    englishLabel: { text: chrome.strings.inEnglish, lang: chrome.lang },
    summary: safety(card.summary),
    expires: live && expires !== null ? safety(format(SAFETY.cardExpires, { time: expires })) : null,
    onlyButton: live ? safety(SAFETY.cardOnlyButton) : null,
    status,
    extra,
    countdown,
    busy: card.status === "in_flight" || card.status === "checking",
    confirm: live
      ? {
          label: safety(card.status === "ready" ? SAFETY.cardConfirm : SAFETY.cardTryAgain),
          enabled: canConfirm,
        }
      : null,
    notNow: live
      ? { label: safety(SAFETY.cardNotNow), enabled: actionable && !otherBusy }
      : null,
  };
}

function sourceView(source: Citation, index: number, chrome: Chrome): SourceView {
  return {
    key: `${source.chunk_id}-${index}`,
    title: { text: source.title, lang: source.language },
    page:
      source.page === null
        ? null
        : { text: format(chrome.strings.page, { page: source.page }), lang: chrome.lang },
    excerpt: { text: source.content, lang: source.language },
  };
}

function entryView(entry: Entry, state: WidgetState, chrome: Chrome): EntryView | null {
  const key = String(entry.id);
  const c = (text: string): Line => ({ text, lang: chrome.lang });
  const name = state.config?.display_name ?? "";
  switch (entry.kind) {
    case "customer": {
      const status =
        entry.status === "not_sent"
          ? c(chrome.strings.notSent)
          : entry.status === "unknown"
            ? c(chrome.strings.mayBeProcessed)
            : null;
      return { key, kind: "customer", who: c(chrome.strings.you), text: { text: entry.text, lang: null }, status };
    }
    case "answer":
      return {
        key,
        kind: "answer",
        who: safety(SAFETY.aiLabel),
        text: { text: entry.text, lang: null },
        sourcesHeading: c(chrome.strings.sources),
        sources: entry.sources.map((source, index) => sourceView(source, index, chrome)),
      };
    case "reply":
      return {
        key,
        kind: "reply",
        who: safety(SAFETY.aiLabel),
        provenance: safety(SAFETY.replyProvenance),
        text: { text: entry.text, lang: null },
      };
    case "confirm": {
      const card = state.cards[entry.actionId];
      return card === undefined ? null : { key, kind: "confirm", card: cardView(card, state, chrome) };
    }
    case "not_answered":
      return { key, kind: "note", text: c(chrome.strings.notAnswered) };
    case "no_answer":
      return { key, kind: "note", text: c(format(chrome.strings.noAnswer, { name })) };
    case "staff_reply":
      return {
        key,
        kind: "staff_reply",
        who: c(format(chrome.strings.staffReply, { name })),
        text: { text: entry.text, lang: null },
      };
  }
}

function noticeView(state: WidgetState, chrome: Chrome, texts: WidgetTexts | null): NoticeView | null {
  const notice = state.notice;
  if (notice === null) return null;
  const s = chrome.strings;
  const c = (text: string): Line => ({ text, lang: chrome.lang });
  const name = state.config?.display_name ?? "";
  const kept = state.draft !== "" ? [c(s.messageKept)] : [];
  const mayStill = state.retryNote ? [c(s.mayStillBeProcessed)] : [];
  let status: Line[] = [];
  let followUp: Line | null = null;
  let countdown: Line | null = null;
  switch (notice.kind) {
    case "working":
      status = [c(s.working)];
      break;
    case "network_error":
      status = [c(s.networkError), ...mayStill, ...kept];
      break;
    case "timeout":
      status = [c(s.timeout), ...mayStill, ...kept];
      break;
    case "session_expired":
      status = [c(s.sessionExpired), ...mayStill, ...kept];
      break;
    case "invalid_message":
      status = [c(s.invalidMessage), ...kept];
      break;
    case "rate_limited": {
      const left = secondsLeft(notice.until, state.now);
      // The countdown is not a live region (it would speak every second); the status line
      // changes once, when sending is possible again.
      status = [c(s.rateLimited), ...(left === 0 ? [c(s.canSendAgain)] : []), ...kept];
      countdown = left > 0 ? c(format(s.retryIn, { seconds: left })) : null;
      break;
    }
    case "server_error":
      status = [c(s.serverError), ...mayStill, ...kept];
      break;
    case "session_unavailable":
      status = [c(s.sessionUnavailable), ...kept];
      break;
    case "handoff": {
      status = notice.reason === "not_covered_language" ? [c(s.notCoveredLanguage)] : [];
      const configured = notice.urgent ? texts?.urgent_follow_up : texts?.handoff_follow_up;
      const allowed = notice.urgent ? notice.queued : notice.followUpAllowed;
      followUp =
        allowed && texts !== null && configured ? { text: configured, lang: texts.language } : null;
      break;
    }
    case "unknown_kind":
      status = [c(s.notAnswered)];
      break;
    case "no_answer":
      status = [c(format(s.noAnswer, { name }))];
      break;
    case "confirm_problem":
      break; // the card says what happened, in the transcript's log
  }
  const contact =
    NON_SUCCESS.has(notice.kind) && texts !== null
      ? {
          heading: c(format(s.contactHeading, { name })),
          lines: texts.contact_fallback.map((text) => ({ text, lang: texts.language })),
        }
      : null;
  return { kind: notice.kind, status, followUp, countdown, contact };
}

// ── The view ────────────────────────────────────────────────────────────────────────────────────

export function deriveView(state: WidgetState): WidgetView {
  const chrome = chromeFor(state.configStatus === "ready" ? state.uiLanguage : (state.requestedLang ?? "en"));
  const c = (text: string): Line => ({ text, lang: chrome.lang });
  const s = chrome.strings;
  const config = state.config;
  const texts = textsFor(config, state.uiLanguage);
  const ready = state.configStatus === "ready" && config !== null;
  const phase: WidgetView["phase"] = !ready
    ? state.configStatus === "unavailable"
      ? "unavailable"
      : "loading"
    : state.ended
      ? "ended"
      : "chat";
  const length = checkLength(state.draft);
  const found = ready ? guidanceLines(state) : null;
  const unreadable = !ready ? state.unavailableGuidance : null;
  const guidance =
    found !== null
      ? guidanceView(found.lines, SAFETY_LANG, found.urgent)
      : unreadable !== null
        ? guidanceView(unreadable.lines, unreadable.lang, false)
        : null;
  const transcript = state.transcript
    .map((entry) => entryView(entry, state, chrome))
    .filter((entry): entry is EntryView => entry !== null);

  return {
    phase,
    uiLang: chrome.lang,
    accent: safeAccent(config?.accent_color ?? null),
    heading: config === null ? null : { text: config.display_name, lang: null },
    aiDisclosure: safety(SAFETY.aiDisclosure),
    notForEmergencies: safety(SAFETY.notForEmergencies),
    safetyInEnglish: chrome.lang !== SAFETY_LANG ? c(s.safetyInEnglish) : null,
    translationNote: chrome.reviewed ? null : c(s.translationUnreviewed),
    talkToPerson: {
      label: safety(SAFETY.talkToPerson),
      visible: ready,
      enabled: ready && !state.handoffInFlight && !state.endInFlight,
    },
    loading: c(s.loading),
    unavailable:
      phase === "unavailable"
        ? { message: c(s.unavailable), help: c(s.unavailableHelp), retry: c(s.retryLoad) }
        : null,
    conversationLabel: c(s.conversation),
    greeting:
      phase === "chat" && texts !== null && state.transcript.length === 0
        ? {
            text: { text: texts.greeting, lang: texts.language },
            suggestionsLabel: c(s.suggestions),
            quickReplies: texts.quick_replies.map((text) => ({
              line: { text, lang: texts.language },
              enabled: canSend(state, text),
            })),
          }
        : null,
    transcript,
    guidance,
    notice: ready ? noticeView(state, chrome, texts) : null,
    composer:
      phase === "chat"
        ? {
            value: state.draft,
            label: c(s.messageLabel),
            counter: c(format(s.counter, { count: length.count, max: length.max })),
            overLimit: length.over > 0 ? c(format(s.overLimit, { over: length.over })) : null,
            hint: state.retryNote && state.draft !== "" ? c(s.mayStillBeProcessed) : null,
            canSend: canSend(state),
            send: c(s.send),
          }
        : null,
    endChat: {
      label: c(s.endChat),
      visible: ready && !state.ended && (state.hasSession || state.transcript.length > 0),
      // Not while a confirm may be running: its outcome would be forgotten (leo's review).
      enabled: !state.endInFlight && !anyCardInFlight(state),
    },
    ended:
      phase === "ended"
        ? {
            message: c(s.ended),
            startNew: c(s.startNew),
            unknownAction: state.endedWithUnknownAction ? safety(SAFETY.cardUnknownHelp) : null,
          }
        : null,
  };
}

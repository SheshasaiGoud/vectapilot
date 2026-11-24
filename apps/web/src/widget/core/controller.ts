/**
 * The widget's controller (M8 PR 4): every request the widget makes and every state it shows.
 * No React and no browser globals: the browser's `fetch`, timers, storage and location are passed
 * in (`ui/browser.ts`), so `node --test` drives the same code with fakes.
 *
 * The rules it keeps (the plan's S8, S14, S16, S18, D11, D15 and leo's confirm table):
 * - **Lazy session** (S14): created on the first send or "Talk to a person", never on page load.
 *   Its token is the only thing written to `sessionStorage`; confirm tokens and the transcript
 *   stay in private fields and state (S18).
 * - **No automatic re-send** (S16, D15): a message is sent only from `send()` or a quick reply,
 *   both user actions. A failure keeps the typed text; after a timeout, a network error or a 5xx
 *   the next send says "your last message may still be processed". The one automatic retry is a
 *   *confirm* retry with the same token after an unknown outcome (leo's table), never a message.
 * - **Guidance first in every state that is not a success** (S16): the view reads `notice`.
 * - **Honest hand-offs** (S8, D11): a follow-up text only when the row was stored, the hand-off is
 *   not urgent and the row is known not to be urgent. The API does not send the row's priority
 *   (PR 2's `HandoffQueued` is `{id, queued}`); the row holds the highest priority of every
 *   hand-off on the session (S17), so it is known only when this view created the session and has
 *   seen every turn's outcome since. A message whose outcome is unknown (timeout, network error,
 *   a 5xx, a turn it cannot read) may have raised it, so from then on no follow-up and no polling
 *   on that session. "Talk to a person" answered while a message was in flight shows no follow-up
 *   either: that message may still raise the row (zara's review). An urgent hand-off drops the
 *   open cards, stops polling and pins its guidance; a card offered after it arrives dropped
 *   (L5 decided (a)). A turn hand-off that carries guidance was not cleared by a safety step
 *   (Z4 (b), decided 2026-11-24, shesha): no follow-up and no polling on that session from then
 *   on, until a new session; the guidance and the contact fallback show instead.
 * - **Confirm** (leo's table, `confirm.ts`): one request at a time; "End chat" waits while a
 *   confirm may be running, and a confirm cut off by a 401 counts as an unknown outcome.
 */

import { request } from "../api/client.ts";
import type { FetchLike, HttpResult, Timers } from "../api/client.ts";
import {
  namesHandoff,
  parseConfig,
  parseError,
  parseGuidance,
  parseHandoffRef,
  parseHandoffStatus,
  parseMessage,
  parseSession,
} from "../api/parse.ts";
import { paths } from "../api/routes.ts";
import type { HandoffRef, Priority, Turn } from "../api/types.ts";
import { DEFAULT_RETRY_AFTER_S, applyConfirmResult } from "./confirm.ts";
import { chooseLanguage, readLocation } from "./location.ts";
import {
  ACTIONABLE_CARD,
  TERMINAL_CARD,
  anyCardInFlight,
  canSend,
  initialState,
} from "./state.ts";
import type { Card, CardStatus, During, Entry, Notice, WidgetState } from "./state.ts";
import type { TokenStore } from "./storage.ts";

/** [proposal] Client bound for the config, session, hand-off, poll and end calls (P7 names only
 * the message and confirm timeouts, which come from the session). Configuration, unmeasured. */
export const BOOTSTRAP_TIMEOUT_MS = 15_000;
/** P7: the widget polls a non-urgent, stored hand-off every 5 s for a staff reply (D7 (a)). */
export const POLL_INTERVAL_MS = 5_000;
/** [proposal] The pause before the one automatic confirm retry after an unknown outcome. */
export const CONFIRM_AUTO_RETRY_MS = 2_000;
const TICK_MS = 1_000;

export interface WidgetDeps {
  fetch: FetchLike;
  timers: Timers;
  tokens: TokenStore;
  now(): number;
  location: { pathname: string; search: string };
}

interface Timeouts {
  messageMs: number;
  confirmMs: number;
}

type SessionOutcome = "ok" | "failed";

/** Card statuses after a confirm answer that is not definite: S16's guidance and contact show. */
const CONFIRM_PROBLEM: ReadonlySet<CardStatus> = new Set([
  "checking",
  "check_manual",
  "not_done",
  "rate_limited",
]);
/** Card statuses that settle an earlier unknown outcome (done, refused, provably not done). */
const SETTLED_CARD: ReadonlySet<CardStatus> = new Set(["executed", "failed", "expired"]);

/** A card whose action may have run without the customer knowing: an unknown outcome that no
 * later answer settled, or a confirm still in flight. */
function unresolved(card: Card): boolean {
  if (card.status === "in_flight" || card.status === "checking") return true;
  return card.unknownOutcome && !SETTLED_CARD.has(card.status);
}

/** S16 for a confirm (zara's review): an answer that is not definite shows the guidance (a 429
 * body's own, else the cached copy) and the contact; a definite one clears that notice again. */
function confirmNotice(current: Notice | null, card: Card, result: HttpResult): Notice | null {
  if (CONFIRM_PROBLEM.has(card.status)) {
    const guidance =
      result.kind === "response" && result.status === 429 ? parseError(result.body).guidance : null;
    return { kind: "confirm_problem", guidance: guidance !== null && guidance.length > 0 ? guidance : null };
  }
  return current?.kind === "confirm_problem" && SETTLED_CARD.has(card.status) ? null : current;
}

export class WidgetController {
  readonly #deps: WidgetDeps;
  #state: WidgetState;
  readonly #listeners = new Set<() => void>();
  // Secrets: never in `#state`, so never in the view, the DOM or a snapshot (S18).
  #token: string | null = null;
  #timeouts: Timeouts | null = null;
  readonly #confirmTokens = new Map<string, string>();
  /** This view created the session and knows every turn's outcome on it, so it has seen every
   * hand-off on it and knows the open row's priority (S17). False for a session adopted from
   * `sessionStorage` and after any message whose outcome is unknown. */
  #seenEveryHandoff = false;
  /** A turn hand-off on this session carried guidance (Z4 (b), decided 2026-11-24, shesha): no
   * safety step cleared that message (`not_covered_language`, `safety_floor_off`, ...), and the
   * guidance itself says not to wait for a reply in the chat. From then on no follow-up and no
   * polling on this session, a later "Talk to a person" included; only a new session clears it. */
  #notCleared = false;
  /** Messages sent and not yet answered (at most one: Send waits). "Talk to a person" answered
   * while one is in flight cannot know the row's priority yet (zara's review). */
  #messagesInFlight = 0;
  #sessionPromise: Promise<SessionOutcome> | null = null;
  /** Bumped when a session is forgotten (End chat, a 401): a result that arrives later belongs to
   * a cleared view and is dropped, so it can neither show text nor store a token. */
  #epoch = 0;
  #lifetime = new AbortController();
  #sessionAbort = new AbortController();
  #ticker: unknown = null;
  #poller: unknown = null;
  #pollInFlight = false;
  readonly #retryTimers = new Map<string, unknown>();
  #nextId = 1;

  constructor(deps: WidgetDeps) {
    this.#deps = deps;
    this.#state = initialState(deps.now());
  }

  // ── Store (for `useSyncExternalStore`) ──────────────────────────────────────────────────────

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  getSnapshot = (): WidgetState => this.#state;

  #update(change: (state: WidgetState) => WidgetState): void {
    this.#state = change(this.#state);
    for (const listener of this.#listeners) listener();
  }

  #patch(patch: Partial<WidgetState>): void {
    this.#update((state) => ({ ...state, ...patch }));
  }

  // ── Lifecycle ───────────────────────────────────────────────────────────────────────────────

  /** Reads the location and loads the configuration (no session: S14). Safe to call again
   * after `dispose()` (React's development double effect). */
  start(): void {
    if (this.#lifetime.signal.aborted) this.#lifetime = new AbortController();
    if (this.#sessionAbort.signal.aborted) this.#sessionAbort = new AbortController();
    if (this.#state.configStatus === "booting" || this.#state.configStatus === "loading") {
      void this.#loadConfig();
    }
    if (this.#state.polling !== null) this.#startPolling(this.#state.polling);
    this.#syncTicker();
  }

  dispose(): void {
    this.#lifetime.abort();
    this.#sessionAbort.abort();
    this.#stopPolling(false);
    this.#stopTicker();
    for (const timer of this.#retryTimers.values()) this.#deps.timers.clearTimeout(timer);
    this.#retryTimers.clear();
  }

  retryConfig(): void {
    if (this.#state.configStatus !== "unavailable") return;
    void this.#loadConfig();
  }

  async #loadConfig(): Promise<void> {
    const { businessId, lang } = readLocation(
      this.#deps.location.pathname,
      this.#deps.location.search,
    );
    if (businessId === null) {
      this.#patch({ configStatus: "unavailable", requestedLang: lang });
      return;
    }
    this.#patch({ configStatus: "loading", businessId, requestedLang: lang });
    const result = await request(this.#deps.fetch, this.#deps.timers, {
      method: "GET",
      path: paths.config(businessId),
      timeoutMs: BOOTSTRAP_TIMEOUT_MS,
      signal: this.#lifetime.signal,
    });
    if (result.kind === "aborted") return;
    const config =
      result.kind === "response" && result.status === 200
        ? parseConfig(result.body, businessId)
        : null;
    if (config === null) {
      // The rest fails closed, but readable guidance is still shown (zara's review).
      const guidance =
        result.kind === "response" && result.status === 200
          ? parseGuidance(result.body, businessId)
          : null;
      this.#patch({ configStatus: "unavailable", unavailableGuidance: guidance });
      return;
    }
    // A token kept from earlier in this tab is used only with the client timeouts it needs
    // (P7); the config route carries them only under PR 2's [proposal]. Otherwise it is dropped
    // and a new session starts lazily.
    const stored = this.#deps.tokens.read(businessId);
    if (stored !== null) {
      if (config.message_timeout_seconds !== null && config.confirm_timeout_seconds !== null) {
        this.#token = stored;
        this.#timeouts = {
          messageMs: config.message_timeout_seconds * 1000,
          confirmMs: config.confirm_timeout_seconds * 1000,
        };
        this.#seenEveryHandoff = false;
      } else {
        this.#deps.tokens.clear(businessId);
      }
    }
    this.#patch({
      configStatus: "ready",
      config,
      uiLanguage: chooseLanguage(lang, config.languages),
      hasSession: this.#token !== null,
      unavailableGuidance: null,
    });
  }

  // ── Composer ────────────────────────────────────────────────────────────────────────────────

  setDraft(text: string): void {
    this.#patch({ draft: text });
  }

  compositionStart(): void {
    this.#patch({ composing: true });
  }

  compositionEnd(): void {
    this.#patch({ composing: false });
  }

  /** Whether a send would go out now (the view disables Send otherwise). */
  canSend(text: string = this.#state.draft): boolean {
    return canSend(this.#state, text);
  }

  /** Sends the composer's text. A user action only (S16): nothing calls it on a timer. */
  send(): Promise<void> {
    return this.#sendText(this.#state.draft, true);
  }

  /** A quick reply is the customer's own message (PR 1's widget file), sent on their click. */
  sendQuickReply(text: string): Promise<void> {
    const offered = this.#texts()?.quick_replies ?? [];
    if (!offered.includes(text)) return Promise.resolve();
    return this.#sendText(text, false);
  }

  async #sendText(text: string, fromComposer: boolean): Promise<void> {
    if (!this.canSend(text)) return;
    this.#messagesInFlight += 1;
    try {
      await this.#deliver(text, fromComposer);
    } finally {
      this.#messagesInFlight -= 1;
    }
  }

  async #deliver(text: string, fromComposer: boolean): Promise<void> {
    const epoch = this.#epoch;
    const entryId = this.#id();
    this.#update((state) => ({
      ...state,
      draft: fromComposer ? "" : state.draft,
      messageInFlight: true,
      retryNote: false,
      notice: { kind: "working" },
      transcript: [...state.transcript, { id: entryId, kind: "customer", text, status: "sending" }],
    }));
    const session = await this.#ensureSession();
    if (epoch !== this.#epoch) return; // the chat was ended meanwhile: nothing comes back
    if (session !== "ok" || this.#token === null || this.#timeouts === null) {
      this.#failSend(entryId, text, "not_sent", false);
      return;
    }
    const result = await request(this.#deps.fetch, this.#deps.timers, {
      method: "POST",
      path: paths.message(),
      timeoutMs: this.#timeouts.messageMs,
      token: this.#token,
      body: { message: text },
      signal: this.#sessionAbort.signal,
    });
    this.#applyMessageResult(entryId, text, result);
  }

  #applyMessageResult(entryId: number, text: string, result: HttpResult): void {
    switch (result.kind) {
      case "aborted":
        return; // the chat was ended or closed meanwhile
      case "timeout":
        this.#outcomeUnknown();
        this.#failSend(entryId, text, "unknown", true, { kind: "timeout", during: "message" });
        return;
      case "network":
        this.#outcomeUnknown();
        this.#failSend(entryId, text, "unknown", true, { kind: "network_error", during: "message" });
        return;
    }
    const { status, body } = result;
    if (status === 200) {
      this.#setEntryStatus(entryId, "sent");
      this.#patch({ messageInFlight: false, notice: null });
      const message = parseMessage(body);
      // A body that names a hand-off row but no readable hand-off turn: the row's priority is
      // unknown, as after a timeout (zara's review).
      if (message.turn.kind !== "handoff" && namesHandoff(body)) this.#outcomeUnknown();
      this.#applyTurn(message.turn, message.handoff);
      return;
    }
    if (status === 401) {
      this.#failSend(entryId, text, "not_sent", false);
      this.#sessionExpired();
      return;
    }
    if (status === 422) {
      this.#failSend(entryId, text, "not_sent", false, { kind: "invalid_message" });
      return;
    }
    if (status === 429) {
      // Send stays enabled during the countdown (zara's review): the server's limiter decides,
      // and a red-flag message skips it (M7 G1), so it still reaches staff as an urgent hand-off.
      this.#failSend(entryId, text, "not_sent", false);
      this.#rateLimited(result, false);
      return;
    }
    // A 5xx may have run the turn (and stored or raised a hand-off, S17), except the session
    // lookup's typed 503, which runs first (S14). Any other 4xx (a widget disabled since:
    // `widget_not_found`) ran nothing.
    const mayHaveRun =
      status >= 500 && !(status === 503 && parseError(body).error === "session_store_unavailable");
    if (mayHaveRun) this.#outcomeUnknown();
    this.#failSend(entryId, text, mayHaveRun ? "unknown" : "not_sent", mayHaveRun, {
      kind: "server_error",
    });
  }

  /** A message may have run without this view seeing its turn: the session's open hand-off may
   * now be urgent (S17), so no follow-up and no polling on this session from here on (D11). */
  #outcomeUnknown(): void {
    this.#seenEveryHandoff = false;
    this.#stopPolling(true);
  }

  /** A send that did not get an answer: the text goes back into an empty composer (S16). */
  #failSend(
    entryId: number,
    text: string,
    status: "not_sent" | "unknown",
    retryNote: boolean,
    notice?: Notice,
  ): void {
    this.#setEntryStatus(entryId, status);
    this.#update((state) => ({
      ...state,
      messageInFlight: false,
      draft: state.draft === "" ? text : state.draft,
      retryNote: retryNote || state.retryNote,
      notice: notice ?? state.notice,
    }));
  }

  #setEntryStatus(entryId: number, status: "sent" | "not_sent" | "unknown"): void {
    this.#update((state) => ({
      ...state,
      transcript: state.transcript.map((entry) =>
        entry.id === entryId && entry.kind === "customer" ? { ...entry, status } : entry,
      ),
    }));
  }

  #applyTurn(turn: Turn, ref: HandoffRef | null): void {
    switch (turn.kind) {
      case "answer": {
        const { outcome, answer, sources } = turn.answer;
        // S9: an answer is shown only when it was answered, has text and has sources.
        if (outcome === "answered" && answer !== null && answer.trim() !== "" && sources.length) {
          this.#append({ id: this.#id(), kind: "answer", text: answer, sources });
          return;
        }
        this.#append({ id: this.#id(), kind: "no_answer" });
        this.#patch({ notice: { kind: "no_answer" } });
        return;
      }
      case "reply":
        this.#append({ id: this.#id(), kind: "reply", text: turn.text });
        return;
      case "confirmation_required": {
        // L5 decided (a) (2026-11-24, shesha; leo's and zara's review). After an urgent hand-off
        // on this chat a card arrives dropped and its token is never kept: nothing after the
        // guidance invites the customer to stay in the chat (D11), and the action expires
        // unexecuted (P3). A new chat ("End chat", then "Start a new chat") can confirm again.
        const dropped = this.#state.urgentSeen;
        if (!dropped) this.#confirmTokens.set(turn.action_id, turn.confirm_token);
        const card: Card = {
          actionId: turn.action_id,
          tool: turn.tool,
          summary: turn.summary,
          expiresAt: turn.expires_at,
          status: dropped ? "dropped" : "ready",
          serverMessage: null,
          unknownOutcome: false,
          autoRetried: false,
          retryAt: null,
        };
        this.#update((state) => ({
          ...state,
          cards: { ...state.cards, [card.actionId]: card },
          transcript: [
            ...state.transcript,
            { id: this.#id(), kind: "confirm", actionId: card.actionId },
          ],
        }));
        return;
      }
      case "handoff":
        this.#append({ id: this.#id(), kind: "not_answered" });
        this.#applyHandoff("turn", turn.reason, turn.priority, turn.guidance, ref);
        return;
      case "unknown":
        // S9: a kind the widget cannot read is the safe hand-off state: guidance and contact,
        // no follow-up, no polling. It may have stored or raised a hand-off, so the row's
        // priority is unknown from here on (zara's review).
        this.#outcomeUnknown();
        this.#append({ id: this.#id(), kind: "not_answered" });
        this.#patch({ notice: { kind: "unknown_kind" } });
        return;
    }
  }

  #append(entry: Entry): void {
    this.#update((state) => ({ ...state, transcript: [...state.transcript, entry] }));
  }

  // ── Hand-offs ───────────────────────────────────────────────────────────────────────────────

  /** "Talk to a person": `customer_asked`, normal, idempotent per session (S17). */
  async talkToPerson(): Promise<void> {
    const state = this.#state;
    if (state.configStatus !== "ready" || state.handoffInFlight || state.endInFlight) return;
    this.#patch({ handoffInFlight: true, ended: false });
    const epoch = this.#epoch;
    const session = await this.#ensureSession();
    if (epoch !== this.#epoch) return;
    if (session !== "ok" || this.#token === null) {
      this.#patch({ handoffInFlight: false });
      return;
    }
    // Settled: no message was in flight when this went out nor when it answered, so every turn
    // that could have raised the row has been seen (zara's review).
    const quietBefore = this.#messagesInFlight === 0;
    const result = await request(this.#deps.fetch, this.#deps.timers, {
      method: "POST",
      path: paths.handoff(),
      timeoutMs: BOOTSTRAP_TIMEOUT_MS,
      token: this.#token,
      body: {},
      signal: this.#sessionAbort.signal,
    });
    if (result.kind === "aborted" || epoch !== this.#epoch) return;
    this.#patch({ handoffInFlight: false });
    if (result.kind === "timeout" || result.kind === "network") {
      const kind = result.kind === "timeout" ? "timeout" : "network_error";
      this.#patch({ notice: { kind, during: "handoff" } });
      return;
    }
    if (result.status === 401) {
      this.#sessionExpired();
      return;
    }
    if (result.status === 429) {
      this.#rateLimited(result, false);
      return;
    }
    if (result.status !== 200 && result.status !== 201) {
      this.#patch({ notice: { kind: "server_error" } });
      return;
    }
    // S17: the session's open row, whatever made it. A body it cannot read counts as "not
    // stored": guidance and contact, never a follow-up.
    const settled = quietBefore && this.#messagesInFlight === 0;
    const ref = parseHandoffRef(result.body);
    this.#applyHandoff("button", "customer_asked", "normal", [], ref, settled);
  }

  #applyHandoff(
    origin: "turn" | "button",
    reason: string,
    priority: Priority,
    turnGuidance: string[],
    ref: HandoffRef | null,
    settled = true,
  ): void {
    const state = this.#state;
    const guidance = turnGuidance.length > 0 ? turnGuidance : (state.config?.guidance ?? []);
    // `urgentSeen`: an urgent hand-off earlier on this session; the row's priority only rises.
    const urgent = priority === "urgent" || reason === "safety" || state.urgentSeen;
    const queued = ref?.queued === true;
    // Z4 (b): the turn's own guidance, never the cached copy (a button hand-off carries none).
    // An earlier row's polling stops, and a staff reply already in flight is dropped (C28 (a)).
    if (turnGuidance.length > 0) {
      this.#notCleared = true;
      this.#stopPolling(true);
    }
    const followUpAllowed =
      queued && !urgent && !this.#notCleared && this.#seenEveryHandoff && settled;
    if (urgent) this.#enterUrgent(guidance);
    this.#patch({
      notice: { kind: "handoff", origin, reason, urgent, queued, followUpAllowed, guidance },
    });
    if (followUpAllowed && ref?.queued === true) this.#startPolling(ref.id);
  }

  /** D11, S8: an urgent hand-off never invites waiting. Polling stops, open cards are dropped
   * (their tokens forgotten: the actions expire unexecuted, P3) and the guidance stays pinned. */
  #enterUrgent(guidance: string[]): void {
    this.#stopPolling(true);
    for (const [actionId, timer] of this.#retryTimers) {
      this.#deps.timers.clearTimeout(timer);
      this.#retryTimers.delete(actionId);
    }
    this.#update((state) => {
      const cards: Record<string, Card> = {};
      for (const [id, card] of Object.entries(state.cards)) {
        const drop = card.status !== "in_flight" && !TERMINAL_CARD.has(card.status);
        if (drop) this.#confirmTokens.delete(id);
        cards[id] = drop ? { ...card, status: "dropped" } : card;
      }
      return {
        ...state,
        cards,
        urgentSeen: true,
        urgentGuidance: guidance.length > 0 ? guidance : state.urgentGuidance,
      };
    });
  }

  #startPolling(handoffId: string): void {
    if (this.#state.urgentSeen || !this.#seenEveryHandoff) return;
    if (this.#poller !== null) this.#deps.timers.clearInterval(this.#poller);
    this.#poller = this.#deps.timers.setInterval(() => void this.#poll(), POLL_INTERVAL_MS);
    if (this.#state.polling !== handoffId) this.#patch({ polling: handoffId });
  }

  #stopPolling(clearState: boolean): void {
    if (this.#poller !== null) this.#deps.timers.clearInterval(this.#poller);
    this.#poller = null;
    if (clearState && this.#state.polling !== null) this.#patch({ polling: null });
  }

  async #poll(): Promise<void> {
    const handoffId = this.#state.polling;
    if (handoffId === null || this.#token === null || this.#pollInFlight) return;
    this.#pollInFlight = true;
    const epoch = this.#epoch;
    const result = await request(this.#deps.fetch, this.#deps.timers, {
      method: "GET",
      path: paths.handoffStatus(handoffId),
      timeoutMs: BOOTSTRAP_TIMEOUT_MS,
      token: this.#token,
      signal: this.#sessionAbort.signal,
    });
    this.#pollInFlight = false;
    if (result.kind !== "response" || epoch !== this.#epoch) return;
    if (this.#state.polling !== handoffId) return;
    if (result.status === 401) {
      this.#sessionExpired();
      return;
    }
    if (result.status === 404) {
      this.#stopPolling(true);
      return;
    }
    if (result.status !== 200) return; // 429, 5xx: try again at the next tick
    const status = parseHandoffStatus(result.body);
    if (status === null || status.id !== handoffId) return;
    if (status.reply !== null) {
      this.#append({ id: this.#id(), kind: "staff_reply", text: status.reply });
      this.#stopPolling(true);
      return;
    }
    if (status.status === "resolved") {
      // Resolved without a reply: no reply is coming, so the follow-up gives way to the contact
      // fallback (S8).
      this.#stopPolling(true);
      this.#update((state) =>
        state.notice?.kind === "handoff"
          ? { ...state, notice: { ...state.notice, followUpAllowed: false } }
          : state,
      );
    }
  }

  // ── Confirmation cards ──────────────────────────────────────────────────────────────────────

  confirm(actionId: string): Promise<void> {
    const state = this.#state;
    const card = state.cards[actionId];
    if (card === undefined || !ACTIONABLE_CARD.has(card.status)) return Promise.resolve();
    if (card.status === "rate_limited" && card.retryAt !== null && state.now < card.retryAt) {
      return Promise.resolve();
    }
    if (state.messageInFlight || state.endInFlight || anyCardInFlight(state)) {
      return Promise.resolve();
    }
    return this.#runConfirm(actionId);
  }

  async #runConfirm(actionId: string): Promise<void> {
    const confirmToken = this.#confirmTokens.get(actionId);
    if (confirmToken === undefined) return;
    if (this.#token === null || this.#timeouts === null) {
      this.#setCard(actionId, (card) => ({ ...card, status: "session_ended" }));
      this.#confirmTokens.delete(actionId);
      return;
    }
    this.#setCard(actionId, (card) => ({ ...card, status: "in_flight" }));
    const epoch = this.#epoch;
    const result = await request(this.#deps.fetch, this.#deps.timers, {
      method: "POST",
      path: paths.confirm(actionId),
      timeoutMs: this.#timeouts.confirmMs,
      token: this.#token,
      body: { confirm_token: confirmToken },
      signal: this.#sessionAbort.signal,
    });
    if (result.kind === "aborted" || epoch !== this.#epoch) return;
    const card = this.#state.cards[actionId];
    if (card === undefined) return;
    const now = this.#deps.now();
    const next = applyConfirmResult(card, result, now);
    const nextCard = next.card;
    if (TERMINAL_CARD.has(nextCard.status)) this.#confirmTokens.delete(actionId);
    // No urgent hand-off can arrive while a confirm runs: Send waits for it, and "Talk to a
    // person" is urgent only after an urgent turn, after which cards arrive dropped.
    this.#update((state) => ({
      ...state,
      now, // a countdown starts at this answer, not at the last tick (leo's review)
      cards: { ...state.cards, [actionId]: nextCard },
      notice: confirmNotice(state.notice, nextCard, result),
    }));
    if (next.effect === "session_expired") this.#sessionExpired();
    if (next.effect === "auto_retry" && nextCard.status === "checking") {
      const timer = this.#deps.timers.setTimeout(() => {
        this.#retryTimers.delete(actionId);
        if (this.#state.cards[actionId]?.status === "checking") void this.#runConfirm(actionId);
      }, CONFIRM_AUTO_RETRY_MS);
      this.#retryTimers.set(actionId, timer);
    }
    if (nextCard.status === "rate_limited") this.#syncTicker();
  }

  /** "Not now": the token is dropped, so the action can never be confirmed and expires (P3). */
  notNow(actionId: string): void {
    const card = this.#state.cards[actionId];
    if (card === undefined || !ACTIONABLE_CARD.has(card.status)) return;
    this.#confirmTokens.delete(actionId);
    this.#setCard(actionId, (current) => ({ ...current, status: "dismissed" }));
  }

  #setCard(actionId: string, change: (card: Card) => Card): void {
    this.#update((state) => {
      const card = state.cards[actionId];
      return card === undefined
        ? state
        : { ...state, cards: { ...state.cards, [actionId]: change(card) } };
    });
  }

  // ── Session ─────────────────────────────────────────────────────────────────────────────────

  #ensureSession(): Promise<SessionOutcome> {
    if (this.#token !== null && this.#timeouts !== null) return Promise.resolve("ok");
    if (this.#sessionPromise === null) {
      // One creation at a time: a send and "Talk to a person" pressed together share it.
      const pending: Promise<SessionOutcome> = this.#createSession().finally(() => {
        if (this.#sessionPromise === pending) this.#sessionPromise = null;
      });
      this.#sessionPromise = pending;
    }
    return this.#sessionPromise;
  }

  async #createSession(): Promise<SessionOutcome> {
    const { businessId, uiLanguage } = this.#state;
    if (businessId === null) return "failed";
    const epoch = this.#epoch;
    const result = await request(this.#deps.fetch, this.#deps.timers, {
      method: "POST",
      path: paths.createSession(),
      timeoutMs: BOOTSTRAP_TIMEOUT_MS,
      body: { business_id: businessId, ui_language: uiLanguage },
      signal: this.#sessionAbort.signal,
    });
    if (result.kind === "aborted" || epoch !== this.#epoch) return "failed";
    if (result.kind === "timeout" || result.kind === "network") {
      const kind = result.kind === "timeout" ? "timeout" : "network_error";
      this.#patch({ notice: { kind, during: "session" } });
      return "failed";
    }
    if (result.status === 429) {
      this.#rateLimited(result);
      return "failed";
    }
    const created =
      result.status === 201 || result.status === 200
        ? parseSession(result.body, businessId)
        : null;
    if (created === null) {
      // S14: Redis down (`limits_unavailable`), Postgres down, or anything else: the guidance
      // and the contact fallback, never "a person will reply".
      this.#patch({ notice: { kind: "session_unavailable" } });
      return "failed";
    }
    this.#token = created.token;
    this.#timeouts = {
      messageMs: created.message_timeout_seconds * 1000,
      confirmMs: created.confirm_timeout_seconds * 1000,
    };
    this.#seenEveryHandoff = true;
    this.#notCleared = false; // Z4 (b): a new session, a new row
    this.#deps.tokens.write(businessId, created.token);
    this.#patch({ hasSession: true });
    return "ok";
  }

  /** Any 401 (S3's one answer): the token is forgotten, cards can no longer be confirmed, the
   * typed text stays, and the next send starts a new session. */
  #sessionExpired(): void {
    this.#forgetSession(); // aborts anything still in flight on this session
    this.#update((state) => {
      const cards: Record<string, Card> = {};
      for (const [id, card] of Object.entries(state.cards)) {
        // A confirm cut off here (in flight, or its automatic retry pending) may have reached
        // the API and run: an unknown outcome, never "nothing was done" (leo's review).
        const cutOff = card.status === "in_flight" || card.status === "checking";
        cards[id] = TERMINAL_CARD.has(card.status)
          ? card
          : { ...card, status: "session_ended", unknownOutcome: card.unknownOutcome || cutOff };
      }
      // A message cut off here may have reached the API: "may still be processed" (D15), and
      // its text goes back into an empty composer (S16).
      const cutOff = state.transcript.find(
        (entry) => entry.kind === "customer" && entry.status === "sending",
      );
      const transcript = state.transcript.map((entry) =>
        entry.kind === "customer" && entry.status === "sending"
          ? { ...entry, status: "unknown" as const }
          : entry,
      );
      return {
        ...state,
        cards,
        transcript,
        draft: state.draft === "" && cutOff?.kind === "customer" ? cutOff.text : state.draft,
        retryNote: state.retryNote || cutOff !== undefined,
        messageInFlight: false,
        handoffInFlight: false,
        hasSession: false,
        notice: { kind: "session_expired" },
      };
    });
  }

  #forgetSession(): void {
    const businessId = this.#state.businessId;
    this.#epoch += 1;
    this.#sessionPromise = null;
    this.#token = null;
    this.#timeouts = null;
    this.#seenEveryHandoff = false;
    this.#confirmTokens.clear();
    if (businessId !== null) this.#deps.tokens.clear(businessId);
    this.#sessionAbort.abort();
    this.#sessionAbort = new AbortController();
    this.#stopPolling(true);
    for (const timer of this.#retryTimers.values()) this.#deps.timers.clearTimeout(timer);
    this.#retryTimers.clear();
  }

  /** "End chat" (S18): revokes the session, then clears everything this page holds. Not while a
   * confirm may be running (leo's review): the page would forget an action that may have run.
   * Kept: the pinned guidance (public configuration, zara's review) and, when a confirm's
   * outcome is still unknown, a line saying so. */
  async endChat(): Promise<void> {
    if (this.#state.endInFlight || this.#state.ended || anyCardInFlight(this.#state)) return;
    const token = this.#token;
    this.#patch({ endInFlight: true });
    if (token !== null) {
      this.#sessionAbort.abort(); // nothing in flight may land in the cleared view
      this.#sessionAbort = new AbortController();
      await request(this.#deps.fetch, this.#deps.timers, {
        method: "DELETE",
        path: paths.endSession(),
        timeoutMs: BOOTSTRAP_TIMEOUT_MS,
        token,
        signal: this.#lifetime.signal,
      });
      // Whatever the answer, the token is forgotten here; an unrevoked session expires on its
      // own at its absolute lifetime (P1).
    }
    this.#forgetSession();
    this.#stopTicker();
    this.#update((state) => ({
      ...initialState(this.#deps.now()),
      configStatus: state.configStatus,
      businessId: state.businessId,
      requestedLang: state.requestedLang,
      uiLanguage: state.uiLanguage,
      config: state.config,
      urgentGuidance: state.urgentGuidance,
      endedWithUnknownAction: Object.values(state.cards).some(unresolved),
      ended: true,
      notice: null,
    }));
  }

  startNew(): void {
    if (!this.#state.ended) return;
    this.#patch({ ended: false, notice: null, endedWithUnknownAction: false });
  }

  // ── Rate limits and the countdown clock ─────────────────────────────────────────────────────

  #rateLimited(result: HttpResult & { kind: "response" }, blockSends = true): void {
    const error = parseError(result.body);
    const seconds = error.retry_after_seconds ?? result.retryAfterSeconds ?? DEFAULT_RETRY_AFTER_S;
    const until = this.#deps.now() + seconds * 1000;
    const guidance = error.guidance !== null && error.guidance.length > 0 ? error.guidance : null;
    this.#patch({
      notice: { kind: "rate_limited", until, guidance },
      now: this.#deps.now(),
      ...(blockSends ? { sendBlockedUntil: until } : {}),
    });
    this.#syncTicker();
  }

  #countdownRunning(): boolean {
    const state = this.#state;
    const now = this.#deps.now();
    if (state.sendBlockedUntil !== null && now < state.sendBlockedUntil) return true;
    if (state.notice?.kind === "rate_limited" && now < state.notice.until) return true;
    return Object.values(state.cards).some(
      (card) => card.status === "rate_limited" && card.retryAt !== null && now < card.retryAt,
    );
  }

  #syncTicker(): void {
    if (this.#countdownRunning()) {
      if (this.#ticker === null) {
        this.#ticker = this.#deps.timers.setInterval(() => this.#tick(), TICK_MS);
      }
    } else {
      this.#stopTicker();
    }
  }

  #tick(): void {
    const now = this.#deps.now();
    this.#update((state) => ({
      ...state,
      now,
      sendBlockedUntil:
        state.sendBlockedUntil !== null && now >= state.sendBlockedUntil
          ? null
          : state.sendBlockedUntil,
    }));
    this.#syncTicker();
  }

  #stopTicker(): void {
    if (this.#ticker !== null) this.#deps.timers.clearInterval(this.#ticker);
    this.#ticker = null;
  }

  // ── Helpers ─────────────────────────────────────────────────────────────────────────────────

  #texts() {
    const { config, uiLanguage } = this.#state;
    return config?.texts.find((texts) => texts.language === uiLanguage) ?? null;
  }

  #id(): number {
    return this.#nextId++;
  }
}

export type { During };

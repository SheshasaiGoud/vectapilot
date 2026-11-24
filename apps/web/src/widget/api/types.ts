/**
 * The widget API's JSON bodies as the widget reads them (M8 PR 4).
 *
 * Mirrors of the API's pydantic models, kept equal by the fixtures in `apps/web/fixtures/widget/`
 * (PR 2 adds the pytest guard that validates every fixture against the models):
 * - PR 1, merged: `widget/models.py` (`WidgetConfigResponse`, `SessionCreated`), the typed error
 *   body `{detail, error}` (`kb/errors.py`) and the 429 body (`guards/limits.py`,
 *   `limited_response`).
 * - PR 2, not merged (`m8/widget-api`, read 2026-11-24): `MessageResponse` (`{turn: AgentResponse,
 *   handoff: HandoffQueued | null}`; `AgentResponse` is `agent/responses.py`, unchanged),
 *   `HandoffQueued` and `HandoffStateResponse` (`widget/models.py`). Widget confirm (PR 2b, not
 *   built yet) answers as the harness route's confirm (`actions/router.py`; `actions/types.py`'s
 *   `ConfirmResponse`). Fields marked [proposal] are asked of PR 2 and optional here: without
 *   them the widget takes the safer path.
 *
 * Every string in these bodies is untrusted text: rendered as React text, never as HTML (S9).
 */

export type Priority = "urgent" | "high" | "normal";

export interface WidgetTexts {
  language: string;
  greeting: string;
  /** Shown only when a non-urgent hand-off was stored (S8); absent: nothing. */
  handoff_follow_up: string | null;
  /** D11 (b): shown only when an urgent hand-off was stored; absent: nothing. */
  urgent_follow_up: string | null;
  contact_fallback: string[];
  quick_replies: string[];
}

/** `GET /v1/widget/{business_id}/config` (files only: answers with Postgres and Redis down). */
export interface WidgetConfig {
  business_id: string;
  display_name: string;
  /** The first one is the default. */
  languages: string[];
  texts: WidgetTexts[];
  /** Verbatim from the business's guard file, one line per item; empty without any (S8). */
  guidance: string[];
  guidance_language: "en";
  accent_color: string | null;
  /**
   * [proposal, PR 2] P7's client timeouts, also here so a session adopted from `sessionStorage`
   * after a reload has them. Absent (PR 1's model): a stored token is not adopted.
   */
  message_timeout_seconds: number | null;
  confirm_timeout_seconds: number | null;
}

/** 201 of `POST /v1/widget/sessions`. */
export interface SessionCreated {
  token: string;
  token_type: "bearer";
  expires_at: string;
  lifetime_seconds: number;
  ui_language: string;
  message_timeout_seconds: number;
  confirm_timeout_seconds: number;
}

/** `retrieval/search.py`'s `Citation`: a retrieved chunk an answer cites. */
export interface Citation {
  chunk_id: string;
  document_id: string;
  source_id: string;
  version: string;
  title: string;
  page: number | null;
  chunk_index: number;
  language: string;
  content_kind: string;
  content: string;
  score: number;
}

/** `answer/types.py`'s `AnswerResponse` (only the fields the widget reads are checked). */
export interface AnswerBody {
  outcome: "answered" | "dont_know" | "handoff";
  answer: string | null;
  sources: Citation[];
}

export interface ReplyTurn {
  kind: "reply";
  trace_id: string;
  text: string;
}

export interface ConfirmationTurn {
  kind: "confirmation_required";
  trace_id: string;
  action_id: string;
  tool: string;
  summary: string;
  summary_language: "en";
  expires_at: string;
  /** Revealed only in this response (ADR 0016 W2): kept in memory, never stored or rendered. */
  confirm_token: string;
}

export interface HandoffTurn {
  kind: "handoff";
  trace_id: string;
  reason: string;
  priority: Priority;
  intent: string | null;
  guidance: string[];
}

export interface AnswerTurn {
  kind: "answer";
  trace_id: string;
  answer: AnswerBody;
}

/** Anything the widget cannot read safely: shown as the safe hand-off state (S9). */
export interface UnknownTurn {
  kind: "unknown";
}

export type Turn = ReplyTurn | ConfirmationTurn | HandoffTurn | AnswerTurn | UnknownTurn;

/**
 * PR 2's `HandoffQueued`: whether the session's open hand-off row exists (S8, S17). `id` is set
 * exactly when `queued`. The row's priority is not sent: it is the highest of every hand-off on
 * the session (S17), so the widget knows it only when it has seen every one of them.
 */
export type HandoffRef = { id: string; queued: true } | { id: null; queued: false };

/** 200 of `POST /v1/widget/messages` (PR 2). */
export interface MessageBody {
  turn: Turn;
  handoff: HandoffRef | null;
}

/** PR 2's `HandoffStateResponse`, 200 of `GET /v1/widget/handoffs/{id}` (D7 (a)). */
export interface HandoffStatusBody {
  id: string;
  status: "open" | "claimed" | "resolved";
  /** The one staff reply, shown as text (S9). */
  reply: string | null;
  replied_at: string | null;
}

/** 200 or 409 of widget confirm: `actions/types.py`'s `ConfirmResponse`. */
export interface ConfirmBody {
  action_id: string;
  tool: string;
  status: "executed" | "failed";
  /** A fixed English sentence chosen by the server (`EXECUTED_MESSAGE`, `FAILURE_MESSAGES`). */
  message: string;
}

/** Every typed error: `{detail, error}`, plus the 429's `retry_after_seconds` and `guidance`. */
export interface ErrorBody {
  error: string | null;
  retry_after_seconds: number | null;
  guidance: string[] | null;
}

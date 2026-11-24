/**
 * Reading the API's bodies defensively (S9): a body that does not have the expected shape is
 * never half-used. A message turn that cannot be read becomes `{kind: "unknown"}`, which the
 * widget shows as the safe hand-off state (guidance and contact, no follow-up, no polling). No
 * schema library: plain type guards over `unknown` (D8 names no new package).
 */

import type {
  AnswerBody,
  Citation,
  ConfirmBody,
  ErrorBody,
  HandoffRef,
  HandoffStatusBody,
  MessageBody,
  Priority,
  SessionCreated,
  Turn,
  WidgetConfig,
  WidgetTexts,
} from "./types.ts";

import { UUID_PATTERN } from "./routes.ts";

type Json = Record<string, unknown>;

/** Ids that go into a URL (an action, a hand-off) must be UUIDs, as the API makes them. */
function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isText(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isStrings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(isString);
}

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function optionalText(value: unknown): string | null | undefined {
  if (value === undefined || value === null) return null;
  return isText(value) ? value : undefined;
}

function optionalSeconds(value: unknown): number | null | undefined {
  if (value === undefined || value === null) return null;
  return isNumber(value) && value > 0 ? value : undefined;
}

const PRIORITIES: readonly Priority[] = ["urgent", "high", "normal"];

function isPriority(value: unknown): value is Priority {
  return PRIORITIES.includes(value as Priority);
}

function parseTexts(value: unknown): WidgetTexts | null {
  if (!isObject(value)) return null;
  const handoff = optionalText(value.handoff_follow_up);
  const urgent = optionalText(value.urgent_follow_up);
  const quick = value.quick_replies === undefined ? [] : value.quick_replies;
  if (
    !isText(value.language) ||
    !isText(value.greeting) ||
    handoff === undefined ||
    urgent === undefined ||
    !isStrings(value.contact_fallback) ||
    !isStrings(quick)
  ) {
    return null;
  }
  return {
    language: value.language,
    greeting: value.greeting,
    handoff_follow_up: handoff,
    urgent_follow_up: urgent,
    contact_fallback: value.contact_fallback,
    quick_replies: quick,
  };
}

/** A language tag the widget writes into `lang` (`en`, `hi`, `te`; a region or script allowed). */
const LANGUAGE_TAG = /^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/;

/**
 * The config route's guidance on its own (zara's review, PR 4): when the rest of the body cannot
 * be read, the widget still shows the business's emergency guidance in the "unavailable" state,
 * with its declared language. `null`: no usable guidance (another business, a wrong shape, none).
 */
export function parseGuidance(
  body: unknown,
  businessId: string,
): { lines: string[]; lang: string } | null {
  if (!isObject(body) || body.business_id !== businessId) return null;
  const { guidance, guidance_language: lang } = body;
  if (!isStrings(guidance) || guidance.length === 0 || !guidance.every(isText)) return null;
  if (!isString(lang) || !LANGUAGE_TAG.test(lang)) return null;
  return { lines: guidance, lang };
}

export function parseConfig(body: unknown, businessId: string): WidgetConfig | null {
  if (!isObject(body) || body.business_id !== businessId) return null;
  const { display_name, languages, texts, guidance, guidance_language, accent_color } = body;
  if (!isText(display_name) || !isStrings(languages) || languages.length === 0) return null;
  if (!Array.isArray(texts) || !isStrings(guidance) || guidance_language !== "en") return null;
  const parsed = texts.map(parseTexts);
  if (parsed.some((entry) => entry === null)) return null;
  const all = parsed as WidgetTexts[];
  if (!languages.every((language) => all.some((entry) => entry.language === language))) {
    return null;
  }
  // Cosmetic: a colour it cannot read becomes the default accent (`safeAccent`), never a reason
  // to drop the whole configuration and its guidance.
  const accent = isString(accent_color) ? accent_color : null;
  const message = optionalSeconds(body.message_timeout_seconds);
  const confirm = optionalSeconds(body.confirm_timeout_seconds);
  if (message === undefined || confirm === undefined) return null;
  return {
    business_id: businessId,
    display_name,
    languages,
    texts: all,
    guidance,
    guidance_language: "en",
    accent_color: accent,
    message_timeout_seconds: message,
    confirm_timeout_seconds: confirm,
  };
}

export function parseSession(body: unknown, businessId: string): SessionCreated | null {
  if (!isObject(body)) return null;
  const { token, expires_at, lifetime_seconds, ui_language } = body;
  const message = body.message_timeout_seconds;
  const confirm = body.confirm_timeout_seconds;
  // The token names its business (`<business_id>.<secret>`, S3): a token for another business
  // is never used, whatever the server sent.
  if (!isText(token) || !token.startsWith(`${businessId}.`) || token.length > 200) return null;
  if (body.token_type !== "bearer" || !isText(expires_at) || !isText(ui_language)) return null;
  if (!isNumber(lifetime_seconds) || !isNumber(message) || !isNumber(confirm)) return null;
  if (message <= 0 || confirm <= 0) return null;
  return {
    token,
    token_type: "bearer",
    expires_at,
    lifetime_seconds,
    ui_language,
    message_timeout_seconds: message,
    confirm_timeout_seconds: confirm,
  };
}

function parseCitation(value: unknown): Citation | null {
  if (!isObject(value)) return null;
  const page = value.page === undefined ? null : value.page;
  if (
    !isText(value.chunk_id) ||
    !isText(value.document_id) ||
    !isText(value.source_id) ||
    !isText(value.version) ||
    !isText(value.title) ||
    !(page === null || isNumber(page)) ||
    !isNumber(value.chunk_index) ||
    !isText(value.language) ||
    !isText(value.content_kind) ||
    !isString(value.content) ||
    !isNumber(value.score)
  ) {
    return null;
  }
  return {
    chunk_id: value.chunk_id,
    document_id: value.document_id,
    source_id: value.source_id,
    version: value.version,
    title: value.title,
    page,
    chunk_index: value.chunk_index,
    language: value.language,
    content_kind: value.content_kind,
    content: value.content,
    score: value.score,
  };
}

function parseAnswer(value: unknown): AnswerBody | null {
  if (!isObject(value)) return null;
  const { outcome, answer, sources } = value;
  if (outcome !== "answered" && outcome !== "dont_know" && outcome !== "handoff") return null;
  if (!(answer === null || isString(answer)) || !Array.isArray(sources)) return null;
  const citations = sources.map(parseCitation);
  if (citations.some((citation) => citation === null)) return null;
  return { outcome, answer, sources: citations as Citation[] };
}

const UNKNOWN: Turn = { kind: "unknown" };

export function parseTurn(value: unknown): Turn {
  if (!isObject(value) || !isText(value.trace_id)) return UNKNOWN;
  const trace_id = value.trace_id;
  switch (value.kind) {
    case "reply":
      return isText(value.text) ? { kind: "reply", trace_id, text: value.text } : UNKNOWN;
    case "confirmation_required": {
      const { action_id, tool, summary, expires_at, confirm_token } = value;
      if (
        !isUuid(action_id) ||
        !isText(tool) ||
        !isText(summary) ||
        value.summary_language !== "en" ||
        !isText(expires_at) ||
        !isText(confirm_token)
      ) {
        return UNKNOWN;
      }
      return {
        kind: "confirmation_required",
        trace_id,
        action_id,
        tool,
        summary,
        summary_language: "en",
        expires_at,
        confirm_token,
      };
    }
    case "handoff": {
      const guidance = value.guidance === undefined ? [] : value.guidance;
      const intent = value.intent === undefined ? null : value.intent;
      if (!isText(value.reason) || !isPriority(value.priority) || !isStrings(guidance)) {
        return UNKNOWN;
      }
      if (!(intent === null || isText(intent))) return UNKNOWN;
      return {
        kind: "handoff",
        trace_id,
        reason: value.reason,
        priority: value.priority,
        intent,
        guidance,
      };
    }
    case "answer": {
      const answer = parseAnswer(value.answer);
      return answer === null ? UNKNOWN : { kind: "answer", trace_id, answer };
    }
    default:
      return UNKNOWN;
  }
}

/** PR 2's `HandoffQueued`; a body it cannot read is `null`, which counts as "not stored". */
export function parseHandoffRef(value: unknown): HandoffRef | null {
  if (!isObject(value)) return null;
  if (value.queued === true && isUuid(value.id)) return { id: value.id, queued: true };
  if (value.queued === false && value.id === null) return { id: null, queued: false };
  return null;
}

/** A message body: a turn that cannot be read is `unknown`; a hand-off ref that cannot be read
 * counts as "not stored" (`null`), so no follow-up text is ever shown on a guess (S8). */
export function parseMessage(body: unknown): MessageBody {
  if (!isObject(body)) return { turn: UNKNOWN, handoff: null };
  const turn = parseTurn(body.turn);
  const handoff = body.handoff === null ? null : parseHandoffRef(body.handoff);
  return { turn, handoff };
}

/** Whether a message body names a hand-off row at all, readable or not: then a turn the widget
 * cannot read as a hand-off leaves the row's priority unknown (zara's review, S17). */
export function namesHandoff(body: unknown): boolean {
  return isObject(body) && body.handoff !== null && body.handoff !== undefined;
}

export function parseHandoffStatus(body: unknown): HandoffStatusBody | null {
  if (!isObject(body) || !isUuid(body.id)) return null;
  const { status } = body;
  if (status !== "open" && status !== "claimed" && status !== "resolved") return null;
  const reply = optionalText(body.reply);
  const repliedAt = optionalText(body.replied_at);
  if (reply === undefined || repliedAt === undefined) return null;
  return { id: body.id, status, reply, replied_at: repliedAt };
}

export function parseConfirm(body: unknown, actionId: string): ConfirmBody | null {
  if (!isObject(body) || body.action_id !== actionId || !isText(body.tool)) return null;
  if (body.status !== "executed" && body.status !== "failed") return null;
  if (!isText(body.message)) return null;
  return { action_id: actionId, tool: body.tool, status: body.status, message: body.message };
}

export function parseError(body: unknown): ErrorBody {
  if (!isObject(body)) return { error: null, retry_after_seconds: null, guidance: null };
  const retry = body.retry_after_seconds;
  return {
    error: isText(body.error) ? body.error : null,
    retry_after_seconds: isNumber(retry) && retry >= 0 ? retry : null,
    guidance: isStrings(body.guidance) ? body.guidance : null,
  };
}

/**
 * The widget's non-safety chrome in English: the reference dictionary. Every other dictionary has
 * exactly these keys with the same `{placeholders}` (`check.ts`, run in CI). Safety text lives in
 * `safety.ts`, English only (D9 (a)).
 */

export const en = {
  loading: "Loading the chat…",
  unavailable: "The chat is not available right now.",
  unavailableHelp: "Please use another way to contact the business.",
  retryLoad: "Try again",
  safetyInEnglish: "Safety messages are shown in English.",
  translationUnreviewed: "This translation has not been reviewed by a native speaker yet.",
  conversation: "Conversation",
  suggestions: "Suggestions",
  messageLabel: "Your message",
  send: "Send",
  counter: "{count} of {max} characters",
  overLimit: "Too long to send: remove {over} characters.",
  working: "Waiting for the assistant…",
  you: "You",
  sources: "Sources",
  page: "page {page}",
  staffReply: "Reply from {name} staff",
  noAnswer: "No answer to this was found in {name}'s information.",
  notAnswered: "The assistant did not answer this message.",
  notCoveredLanguage: "The assistant cannot read messages in this language yet.",
  contactHeading: "Other ways to reach {name}",
  networkError: "The connection to the chat was lost.",
  timeout: "No reply arrived in time.",
  mayStillBeProcessed: "Your last message may still be processed.",
  sessionExpired: "This chat session has ended. Your next message starts a new one.",
  invalidMessage: "This message could not be sent. Check it, then send again.",
  rateLimited: "Too many messages in a short time.",
  retryIn: "You can send again in {seconds} s.",
  canSendAgain: "You can send again now.",
  serverError: "Something went wrong on our side, and this message was not answered.",
  sessionUnavailable: "The chat cannot start right now.",
  messageKept: "Your message is still in the box below.",
  notSent: "Not sent",
  mayBeProcessed: "May still be processed",
  endChat: "End chat",
  ended: "Chat ended. The conversation was cleared from this page.",
  startNew: "Start a new chat",
  inEnglish: "In English",
} as const;

export type ChromeKey = keyof typeof en;
export type Dictionary = Readonly<Record<ChromeKey, string>>;

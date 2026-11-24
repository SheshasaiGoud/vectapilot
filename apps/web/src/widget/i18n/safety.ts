/**
 * Safety-relevant widget text (D9 (a)): English only, always rendered with `lang="en"`, in every
 * UI language, until a native reviewer signs off a translation. In a Hindi or Telugu UI, the
 * chrome line `safetyInEnglish` labels them. No unreviewed translation ever carries one of these.
 *
 * What counts as safety text here: the AI disclosure, "Talk to a person", "not for emergencies",
 * the provenance of a model's reply, the guidance headings, and every line of the confirmation
 * card (its summary is English too: `summary_language: "en"`, P6).
 *
 * Two guidance headings (zara's review): the guidance is shown in every state that is not a
 * success (S16), so outside an urgent hand-off its heading makes it conditional; shown on every
 * network blip as an unconditional instruction, it would teach people to skip it.
 *
 * The guidance itself is never here: it comes verbatim from the business's guard file through the
 * config route (S8), English only (Q8).
 */

export const SAFETY_LANG = "en";

export const SAFETY = {
  aiDisclosure: "You are chatting with an AI assistant, not a person.",
  aiLabel: "AI assistant",
  notForEmergencies: "This chat is not for emergencies.",
  talkToPerson: "Talk to a person",
  replyProvenance: "Written by the AI assistant. Not taken from the business's documents.",
  /** An urgent or `safety` hand-off's guidance, and the guidance pinned after it. */
  guidanceHeadingUrgent: "Safety information",
  /** The guidance in every other state that is not a success. */
  guidanceHeading: "If this is an emergency:",
  cardHeading: "Please confirm",
  cardOnlyButton: "Only the Confirm button confirms. Typing “yes” sends a new message.",
  cardExpires: "Expires at {time}",
  cardConfirm: "Confirm",
  cardNotNow: "Not now",
  cardTryAgain: "Try again",
  cardConfirming: "Confirming…",
  cardExpired: "Expired. Nothing was done.",
  cardNotDone: "Not done yet. You can try again.",
  cardChecking: "We are checking whether this was done.",
  cardStillChecking: "We could not check yet. Try again, or talk to a person.",
  cardRateLimited: "Too many attempts. You can try again shortly.",
  /** Shown next to `cardRateLimited`, never announced (it changes every second). */
  cardRetryIn: "Try again in {seconds} s.",
  cardCanTryAgain: "You can try again now.",
  cardSessionEnded: "Your chat session has ended, so this can no longer be confirmed here.",
  cardUnknownHelp: "We could not check whether this was done. Talk to a person to check.",
  cardDismissed: "Not confirmed. Nothing was done.",
  cardDropped: "Cancelled because of the safety information. Nothing was done.",
} as const;

export type SafetyKey = keyof typeof SAFETY;

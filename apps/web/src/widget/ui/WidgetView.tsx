/**
 * The widget's markup (M8 PR 4): a pure function of the view model, so tests render it with
 * `react-dom/server` and check the HTML (S9's XSS fixtures, S16's guidance-first order).
 *
 * Text only (S9): every string from the API, the configuration or the customer is a React text
 * child, never HTML: no `dangerouslySetInnerHTML` (ESLint `react/no-danger: error`), no markdown,
 * no auto-linking, no URL built from untrusted text. Every text carries its own `lang`.
 *
 * Accessibility (S15, WCAG 2.2 AA): native buttons and a labelled textarea, the transcript as a
 * `role="log"`, one `role="status"` that is always present, the guidance as `role="alert"` with
 * `lang="en"` and no animation, "Talk to a person" in the same place in every state (3.2.6),
 * targets at least 44 px high (2.5.8), no dragging (2.5.7), and normal document flow so the page
 * reflows at 320 px and 200 % zoom (no inner scroll box can hide a focused control, 2.4.11).
 * `overflow-wrap: anywhere` on the root, inherited by every text: a long unbroken string (a URL,
 * a payload) can break inside a flex row too (a quick reply, a citation chip), so untrusted text
 * never makes the page scroll sideways (1.4.10; found by the XSS browser test).
 */

import type { CSSProperties, KeyboardEvent, ReactNode } from "react";

import type { CardView, EntryView, Line, NoticeView, WidgetView as View } from "../core/view.ts";

export interface WidgetActions {
  setDraft(text: string): void;
  compositionStart(): void;
  compositionEnd(): void;
  send(): void;
  sendQuickReply(text: string): void;
  talkToPerson(): void;
  confirm(actionId: string): void;
  notNow(actionId: string): void;
  endChat(): void;
  startNew(): void;
  retryConfig(): void;
}

function L({ line, className }: { line: Line; className?: string }) {
  return (
    <span lang={line.lang ?? undefined} className={className}>
      {line.text}
    </span>
  );
}

function P({ line, className, id }: { line: Line; className?: string; id?: string }) {
  return (
    <p lang={line.lang ?? undefined} className={className} id={id}>
      {line.text}
    </p>
  );
}

const BUTTON =
  "inline-flex min-h-11 items-center justify-center rounded-md px-4 py-2 text-sm font-semibold " +
  "disabled:cursor-not-allowed disabled:opacity-60";
const PRIMARY = `${BUTTON} bg-[var(--vp-accent)] text-white`;
const SECONDARY = `${BUTTON} border border-gray-600 bg-white text-gray-900`;

function Button({
  line,
  onClick,
  enabled = true,
  primary = false,
  type = "button",
  id,
}: {
  line: Line;
  onClick?: () => void;
  enabled?: boolean;
  primary?: boolean;
  type?: "button" | "submit";
  id?: string;
}) {
  return (
    <button
      id={id}
      type={type}
      lang={line.lang ?? undefined}
      onClick={onClick}
      disabled={!enabled}
      className={primary ? PRIMARY : SECONDARY}
    >
      {line.text}
    </button>
  );
}

function Who({ line }: { line: Line }) {
  return <P line={line} className="text-xs font-semibold uppercase tracking-wide text-gray-700" />;
}

function Card({ card, actions }: { card: CardView; actions: WidgetActions }) {
  const headingId = `vp-card-${card.actionId}`;
  return (
    // `tabIndex={-1}` and `data-vp-focus-return`: when its buttons go (an answer came), keyboard
    // focus returns here instead of to the page (`WidgetRoot`, WCAG 2.4.3).
    <section
      id={`vp-cardbox-${card.actionId}`}
      tabIndex={-1}
      data-vp-focus-return=""
      aria-labelledby={headingId}
      aria-busy={card.busy || undefined}
      className="rounded-lg border-2 border-gray-700 bg-gray-50 p-3"
    >
      <h2 id={headingId} className="text-base font-semibold">
        <L line={card.heading} /> <L line={card.englishLabel} className="text-xs font-normal text-gray-700" />
      </h2>
      <P line={card.summary} className="mt-2 whitespace-pre-line break-words" />
      {card.expires && <P line={card.expires} className="mt-2 text-sm text-gray-700" />}
      {card.onlyButton && <P line={card.onlyButton} className="mt-1 text-sm text-gray-700" />}
      {card.status && <P line={card.status} className="mt-2 font-medium" />}
      {card.countdown && (
        // Inside the transcript's live log: shown, never announced every second (leo's review).
        <p aria-hidden="true" lang={card.countdown.lang ?? undefined} className="mt-1 text-sm">
          {card.countdown.text}
        </p>
      )}
      {card.extra && <P line={card.extra} className="mt-1 font-medium" />}
      {(card.confirm || card.notNow) && (
        <div className="mt-3 flex flex-wrap gap-2">
          {card.confirm && (
            <Button
              line={card.confirm.label}
              enabled={card.confirm.enabled}
              primary
              onClick={() => actions.confirm(card.actionId)}
            />
          )}
          {card.notNow && (
            <Button
              line={card.notNow.label}
              enabled={card.notNow.enabled}
              onClick={() => actions.notNow(card.actionId)}
            />
          )}
        </div>
      )}
    </section>
  );
}

function Entry({ entry, actions }: { entry: EntryView; actions: WidgetActions }): ReactNode {
  switch (entry.kind) {
    case "customer":
      return (
        <div className="ml-8 rounded-lg bg-gray-100 p-3">
          <Who line={entry.who} />
          <P line={entry.text} className="whitespace-pre-line break-words" />
          {entry.status && <P line={entry.status} className="mt-1 text-sm font-medium text-gray-800" />}
        </div>
      );
    case "answer":
      return (
        <div className="mr-8 rounded-lg border border-gray-300 p-3">
          <Who line={entry.who} />
          <P line={entry.text} className="whitespace-pre-line break-words" />
          <P line={entry.sourcesHeading} className="mt-2 text-sm font-semibold" />
          <ul className="mt-1 flex flex-col gap-2">
            {entry.sources.map((source) => (
              <li key={source.key}>
                <details className="rounded-md border border-gray-500">
                  <summary className="flex min-h-11 cursor-pointer flex-wrap items-center gap-1 px-3 py-2 text-sm">
                    <L line={source.title} className="min-w-0 font-medium" />
                    {source.page && (
                      <>
                        {", "}
                        <L line={source.page} />
                      </>
                    )}
                  </summary>
                  <P line={source.excerpt} className="whitespace-pre-line break-words border-t border-gray-300 px-3 py-2 text-sm" />
                </details>
              </li>
            ))}
          </ul>
        </div>
      );
    case "reply":
      return (
        <div className="mr-8 rounded-lg border border-gray-300 p-3">
          <Who line={entry.who} />
          <P line={entry.text} className="whitespace-pre-line break-words" />
          <P line={entry.provenance} className="mt-1 text-xs text-gray-700" />
        </div>
      );
    case "confirm":
      return <Card card={entry.card} actions={actions} />;
    case "note":
      return <P line={entry.text} className="text-sm text-gray-700" />;
    case "staff_reply":
      return (
        <div className="mr-8 rounded-lg border-2 border-gray-700 p-3">
          <Who line={entry.who} />
          <P line={entry.text} className="whitespace-pre-line break-words" />
        </div>
      );
  }
}

function Status({ notice }: { notice: NoticeView | null }) {
  // Always in the DOM, so a change is announced (a live region added with its text may not be).
  return (
    <div role="status" className="flex flex-col gap-1">
      {notice?.status.map((line, index) => (
        <P key={`${notice.kind}-${index}`} line={line} className="font-medium" />
      ))}
      {notice?.kind === "working" && (
        <span aria-hidden="true" className="flex gap-1 text-lg leading-none text-gray-700">
          <span className="vp-dot">•</span>
          <span className="vp-dot">•</span>
          <span className="vp-dot">•</span>
        </span>
      )}
      {notice?.followUp && <P line={notice.followUp} />}
    </div>
  );
}

/** S8, S16: first in its area, verbatim, in its own language (English until a reviewed safety
 * file exists), `role="alert"`, never animated (no animation class, ever). */
function Guidance({ guidance }: { guidance: NonNullable<View["guidance"]> }) {
  return (
    <div
      id="vp-guidance"
      role="alert"
      lang={guidance.lang}
      data-vp-urgent={guidance.urgent ? "" : undefined}
      className="rounded-lg border-2 border-amber-800 bg-amber-50 p-3 text-amber-950"
    >
      <h2 lang={guidance.heading.lang ?? undefined} className="font-semibold">
        {guidance.heading.text}
      </h2>
      {guidance.lines.map((line, index) => (
        <p key={index} className="mt-1 break-words">
          {line.text}
        </p>
      ))}
    </div>
  );
}

function onEnter(event: KeyboardEvent<HTMLTextAreaElement>, actions: WidgetActions): void {
  // Enter sends; Shift+Enter is a new line. Never while an IME composition is open: there Enter
  // commits the composition (Safari reports that keydown with keyCode 229).
  if (event.key !== "Enter" || event.shiftKey) return;
  if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
  event.preventDefault();
  actions.send();
}

export function WidgetView({ view, actions }: { view: View; actions: WidgetActions }) {
  const style = { "--vp-accent": view.accent } as CSSProperties;
  const composer = view.composer;
  const describedBy = composer
    ? ["vp-counter", composer.overLimit && "vp-over", composer.hint && "vp-hint"]
        .filter(Boolean)
        .join(" ")
    : undefined;
  return (
    <div lang={view.uiLang} style={style} className="mx-auto flex min-h-dvh max-w-2xl flex-col bg-white text-gray-800 [overflow-wrap:anywhere]">
      <header className="border-b border-gray-300 px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h1 className="break-words text-lg font-semibold text-gray-900">
              {view.heading ? <L line={view.heading} /> : <L line={view.loading} />}
            </h1>
            <P line={view.aiDisclosure} className="text-sm text-gray-700" />
            <P line={view.notForEmergencies} className="text-sm font-medium text-gray-900" />
          </div>
          {view.talkToPerson.visible && (
            <Button
              line={view.talkToPerson.label}
              enabled={view.talkToPerson.enabled}
              primary
              onClick={actions.talkToPerson}
            />
          )}
        </div>
        {view.safetyInEnglish && <P line={view.safetyInEnglish} className="mt-2 text-sm text-gray-700" />}
        {view.translationNote && <P line={view.translationNote} className="text-sm text-gray-700" />}
      </header>

      <main className="flex flex-1 flex-col gap-3 px-4 py-3">
        {view.phase === "loading" && (
          <div className="flex flex-col items-start gap-2">
            {view.guidance && <Guidance guidance={view.guidance} />}
            <p role="status" lang={view.loading.lang ?? undefined}>
              {view.loading.text}
            </p>
          </div>
        )}

        {view.unavailable && (
          <div className="flex flex-col items-start gap-2">
            {/* The configuration could not be read, but its guidance could (zara's review). */}
            {view.guidance && <Guidance guidance={view.guidance} />}
            <P line={view.unavailable.message} className="font-medium" />
            <P line={view.unavailable.help} />
            <Button line={view.unavailable.retry} onClick={actions.retryConfig} />
          </div>
        )}

        {(view.phase === "chat" || view.phase === "ended") && (
          <div role="log" aria-label={view.conversationLabel.text}>
            <ol className="flex flex-col gap-3">
              {view.greeting && (
                <li>
                  <div className="mr-8 rounded-lg border border-gray-300 p-3">
                    <P line={view.greeting.text} className="whitespace-pre-line break-words" />
                  </div>
                </li>
              )}
              {view.transcript.map((entry) => (
                <li key={entry.key}>
                  <Entry entry={entry} actions={actions} />
                </li>
              ))}
            </ol>
          </div>
        )}

        {view.greeting && view.greeting.quickReplies.length > 0 && (
          <div role="group" aria-label={view.greeting.suggestionsLabel.text} className="flex flex-wrap gap-2">
            {view.greeting.quickReplies.map((reply) => (
              <Button
                key={reply.line.text}
                line={reply.line}
                enabled={reply.enabled}
                onClick={() => actions.sendQuickReply(reply.line.text)}
              />
            ))}
          </div>
        )}

        {view.phase === "chat" && (
          <div className="flex flex-col gap-3">
            {view.guidance && <Guidance guidance={view.guidance} />}
            <Status notice={view.notice} />
            {view.notice?.countdown && <P line={view.notice.countdown} className="text-sm" />}
            {view.notice?.contact && (
              <div>
                <h2 className="text-sm font-semibold" lang={view.notice.contact.heading.lang ?? undefined}>
                  {view.notice.contact.heading.text}
                </h2>
                <ul className="mt-1 list-disc pl-5 text-sm">
                  {view.notice.contact.lines.map((line, index) => (
                    <li key={index} lang={line.lang ?? undefined} className="break-words">
                      {line.text}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        {view.ended && (
          <div className="flex flex-col items-start gap-2">
            {/* After an urgent hand-off the guidance stays (zara's review): it is the business's
                public configuration, and the customer may be leaving the chat to act on it. */}
            {view.guidance && <Guidance guidance={view.guidance} />}
            <p role="status" lang={view.ended.message.lang ?? undefined}>
              {view.ended.message.text}
            </p>
            {view.ended.unknownAction && <P line={view.ended.unknownAction} className="font-medium" />}
            <Button id="vp-start-new" line={view.ended.startNew} onClick={actions.startNew} />
          </div>
        )}
      </main>

      {composer && (
        <form
          id="vp-composer"
          className="border-t border-gray-300 px-4 py-3"
          onSubmit={(event) => {
            event.preventDefault();
            actions.send();
          }}
        >
          <label htmlFor="vp-message" lang={composer.label.lang ?? undefined} className="block text-sm font-medium">
            {composer.label.text}
          </label>
          <textarea
            id="vp-message"
            rows={3}
            value={composer.value}
            aria-describedby={describedBy}
            aria-invalid={composer.overLimit ? true : undefined}
            onChange={(event) => actions.setDraft(event.target.value)}
            onCompositionStart={actions.compositionStart}
            onCompositionEnd={actions.compositionEnd}
            onKeyDown={(event) => onEnter(event, actions)}
            className="mt-1 block w-full resize-y rounded-md border border-gray-600 p-2 text-base"
          />
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
            <P line={composer.counter} id="vp-counter" className="text-xs text-gray-700" />
            <Button line={composer.send} type="submit" enabled={composer.canSend} primary />
          </div>
          {composer.overLimit && <P line={composer.overLimit} id="vp-over" className="mt-1 text-sm font-medium text-red-800" />}
          {composer.hint && <P line={composer.hint} id="vp-hint" className="mt-1 text-sm" />}
        </form>
      )}

      <footer className="flex justify-end border-t border-gray-300 px-4 py-2">
        {view.endChat.visible && (
          <Button line={view.endChat.label} enabled={view.endChat.enabled} onClick={actions.endChat} />
        )}
      </footer>
    </div>
  );
}

"use client";

/**
 * The widget's client root (M8 PR 4): creates the controller in the browser, subscribes with
 * `useSyncExternalStore`, and hands the derived view to `WidgetView`.
 *
 * Prerendered at build time as the loading state (the server snapshot), which is also what the
 * first client render shows, so hydration matches. The controller starts in an effect: it reads
 * the location and fetches the configuration, and creates no session (S14).
 */

import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { WidgetController } from "../core/controller.ts";
import { initialState } from "../core/state.ts";
import type { WidgetState } from "../core/state.ts";
import { deriveView } from "../core/view.ts";
import { browserDeps } from "./browser.ts";
import { WidgetView } from "./WidgetView.tsx";
import type { WidgetActions } from "./WidgetView.tsx";

const BOOT: WidgetState = initialState(0);
const bootSnapshot = () => BOOT;
const noSubscription = () => () => {};

const NO_ACTIONS: WidgetActions = {
  setDraft() {},
  compositionStart() {},
  compositionEnd() {},
  send() {},
  sendQuickReply() {},
  talkToPerson() {},
  confirm() {},
  notNow() {},
  endChat() {},
  startNew() {},
  retryConfig() {},
};

export function WidgetRoot() {
  const [controller] = useState(() =>
    typeof window === "undefined" ? null : new WidgetController(browserDeps()),
  );
  const state = useSyncExternalStore(
    controller?.subscribe ?? noSubscription,
    controller?.getSnapshot ?? bootSnapshot,
    bootSnapshot,
  );
  const view = useMemo(() => deriveView(state), [state]);

  useEffect(() => {
    if (controller === null) return;
    controller.start();
    return () => controller.dispose();
  }, [controller]);

  useEffect(() => {
    document.documentElement.lang = view.uiLang;
  }, [view.uiLang]);

  const entries = view.transcript.length;
  const noticeKind = view.notice?.kind ?? null;
  const hasGuidance = view.guidance !== null;
  useEffect(() => {
    // Keep the newest message and the composer in view, then the guidance, which wins when both
    // do not fit (a phone, S8). `block: "nearest"` moves nothing that is already visible; never a
    // smooth scroll (reduced motion).
    if (entries === 0 && noticeKind === null) return;
    document.getElementById("vp-composer")?.scrollIntoView({ block: "nearest" });
    if (hasGuidance) document.getElementById("vp-guidance")?.scrollIntoView({ block: "nearest" });
  }, [entries, noticeKind, hasGuidance]);

  // Keyboard focus never falls back to the page when the focused control goes away (WCAG
  // 2.4.3): a card's buttons after its answer (to the card), "End chat" (to "Start a new chat"),
  // "Start a new chat" or a used quick reply (to the message box).
  const lastFocus = useRef<{ element: Element; fallback: string | null } | null>(null);
  useEffect(() => {
    const remember = (event: FocusEvent) => {
      if (!(event.target instanceof Element)) return;
      const box = event.target.closest("[data-vp-focus-return]");
      lastFocus.current = { element: event.target, fallback: box?.id ?? null };
    };
    document.addEventListener("focusin", remember);
    return () => document.removeEventListener("focusin", remember);
  }, []);
  useEffect(() => {
    const last = lastFocus.current;
    if (last === null || last.element.isConnected) return;
    const active = document.activeElement;
    if (active !== null && active !== document.body) return;
    const target =
      (last.fallback !== null ? document.getElementById(last.fallback) : null) ??
      document.getElementById("vp-start-new") ??
      document.getElementById("vp-message");
    target?.focus();
  }, [view]);

  const actions = useMemo<WidgetActions>(
    () =>
      controller === null
        ? NO_ACTIONS
        : {
            setDraft: (text) => controller.setDraft(text),
            compositionStart: () => controller.compositionStart(),
            compositionEnd: () => controller.compositionEnd(),
            send: () => void controller.send(),
            sendQuickReply: (text) => void controller.sendQuickReply(text),
            talkToPerson: () => void controller.talkToPerson(),
            confirm: (actionId) => void controller.confirm(actionId),
            notNow: (actionId) => controller.notNow(actionId),
            endChat: () => void controller.endChat(),
            startNew: () => controller.startNew(),
            retryConfig: () => controller.retryConfig(),
          },
    [controller],
  );

  return <WidgetView view={view} actions={actions} />;
}

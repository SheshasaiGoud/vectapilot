"use client";

import { AnimatePresence, useInView } from "motion/react";
import * as m from "motion/react-m";
import { useEffect, useRef, useState } from "react";

import type { Status } from "@/lib/types";

const TYPING_MS = 1100;
const SHOWING_MS = 3800;

/**
 * The teammate's speech bubble. Its liveliness mirrors their status:
 * active → types and cycles catchphrases · next → one motto · standby → asleep.
 */
export function QuipBubble({
  quips,
  status,
  offset = 0,
}: {
  quips: string[];
  status: Status;
  /** Staggers the starting phrase so cards don't speak in unison. */
  offset?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref, { margin: "-40px" });
  const [state, setState] = useState({ index: offset % quips.length, typing: true });
  const cycling = status.kind === "active";

  useEffect(() => {
    // Only animate while visible: no timers running for off-screen cards.
    if (!cycling || !inView) return;
    const timer = setTimeout(
      () =>
        setState((s) =>
          s.typing ? { ...s, typing: false } : { index: (s.index + 1) % quips.length, typing: true },
        ),
      state.typing ? TYPING_MS : SHOWING_MS,
    );
    return () => clearTimeout(timer);
  }, [cycling, inView, state, quips.length]);

  return (
    <div
      ref={ref}
      className="relative mt-4 flex min-h-[3.75rem] items-center rounded-2xl rounded-tl-sm bg-white/[0.04] px-3.5 py-2.5 text-sm ring-1 ring-white/10"
    >
      {status.kind === "standby" ? (
        <p className="flex items-center gap-2 text-mist-400">
          <span className="inline-block animate-float" aria-hidden>
            💤
          </span>
          Resting until their milestone starts
        </p>
      ) : status.kind === "next" ? (
        <p className="text-mist-200">“{quips[0]}”</p>
      ) : (
        <AnimatePresence mode="wait" initial={false}>
          {state.typing ? (
            <m.span
              key="typing"
              className="flex gap-1"
              aria-label="typing"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
            >
              {[0, 1, 2].map((dot) => (
                <m.span
                  key={dot}
                  className="size-1.5 rounded-full bg-mist-300"
                  animate={{ y: [0, -4, 0], opacity: [0.4, 1, 0.4] }}
                  transition={{ duration: 0.9, repeat: Infinity, delay: dot * 0.15 }}
                />
              ))}
            </m.span>
          ) : (
            <m.p
              key={state.index}
              className="text-mist-50"
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: 0.25 }}
            >
              “{quips[state.index]}”
            </m.p>
          )}
        </AnimatePresence>
      )}
    </div>
  );
}

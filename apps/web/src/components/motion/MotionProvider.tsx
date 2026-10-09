"use client";

import { LazyMotion, MotionConfig } from "motion/react";
import type { ReactNode } from "react";

// Animation features download after the page is interactive instead of in the first bundle.
const loadFeatures = () => import("./features").then((mod) => mod.default);

/**
 * - `LazyMotion` + `m.*` components keep Motion out of the initial JavaScript; `strict` makes
 *   any accidental use of the heavier `motion.*` components throw during development.
 * - `reducedMotion="user"` makes every Motion animation follow the operating-system
 *   "reduce motion" setting: transforms are skipped, opacity fades remain.
 */
export function MotionProvider({ children }: { children: ReactNode }) {
  return (
    <LazyMotion features={loadFeatures} strict>
      <MotionConfig reducedMotion="user">{children}</MotionConfig>
    </LazyMotion>
  );
}

import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./product.css";

/**
 * The product pages' root layout (M8 PR 4): only the product build sees it
 * (`pageExtensions` in `next.config.ts`). Lean on purpose: no web fonts (the system stack covers
 * Latin, Devanagari and Telugu), no Motion, no team-site styles. `lang` starts as English and the
 * widget sets the visitor's UI language once it has read its configuration.
 */
export const metadata: Metadata = {
  title: "Chat",
  robots: { index: false, follow: false },
};

export default function ProductLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

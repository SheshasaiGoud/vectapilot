import path from "node:path";

import type { NextConfig } from "next";

/**
 * Two builds from one app (M8 PR 4, D2 (a) with (i); `docs/adr/0019`):
 *
 * - **The team site** (`npm run build`, the default): `out/`, published to GitHub Pages. It sees
 *   only the usual route files (`page.tsx`, `layout.tsx`), so it can never contain a product page.
 * - **The product pages** (`npm run build:ui`, `VECTAPILOT_UI_BUILD=product`): `out-ui/`, the
 *   folder the API serves read-only as its `ui_dir` (PR 2). It sees only `*.product.tsx` route
 *   files: `pageExtensions` is the switch, because Next.js matches `page.<ext>` exactly
 *   (`next/dist/server/lib/find-page-file.js`, `createValidFileMatcher`), so `page.product.tsx`
 *   is a page in this build and an ordinary module in the other. With `output: "export"`, a
 *   custom `distDir` names the export folder (`next/dist/export/utils.js`,
 *   `hasCustomExportOutput`); build files stay in `.next/`, so run the two builds one after the
 *   other, never in parallel.
 *
 * `scripts/check-site-out.mjs` proves the split on every CI run: `out/` holds no product page and
 * no API route, and `out-ui/` holds the widget.
 */
const PRODUCT = process.env.VECTAPILOT_UI_BUILD === "product";
const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

if (process.env.VECTAPILOT_UI_BUILD !== undefined && !PRODUCT) {
  throw new Error('VECTAPILOT_UI_BUILD must be "product" or unset');
}
if (PRODUCT && BASE_PATH !== "") {
  // The API serves the product pages at its own root (`/widget/{business_id}`), never under the
  // GitHub Pages prefix.
  throw new Error("the product build takes no NEXT_PUBLIC_BASE_PATH");
}

const nextConfig: NextConfig = {
  // Static HTML export for free hosting on GitHub Pages (docs/adr/0002).
  // Note: the template's `cacheComponents` / `partialPrefetching` are left off on purpose —
  // they enable Partial Prerendering, which needs a server and fails with `output: "export"`.
  output: "export",
  // "/vectapilot" when built for GitHub Pages, "" locally. Inlined at build time.
  basePath: BASE_PATH,
  ...(PRODUCT ? { pageExtensions: ["product.tsx", "product.ts"], distDir: "out-ui" } : {}),
  // No image-optimisation server in a static export; our avatars are already-small SVGs.
  images: { unoptimized: true },
  turbopack: {
    // The repo root (this is a monorepo). Set explicitly so Turbopack never guesses a
    // folder outside the repository.
    root: path.join(__dirname, "../.."),
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;

import path from "node:path";

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Static HTML export for free hosting on GitHub Pages (docs/adr/0002).
  // Note: the template's `cacheComponents` / `partialPrefetching` are left off on purpose —
  // they enable Partial Prerendering, which needs a server and fails with `output: "export"`.
  output: "export",
  // "/vectapilot" when built for GitHub Pages, "" locally. Inlined at build time.
  basePath: process.env.NEXT_PUBLIC_BASE_PATH ?? "",
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

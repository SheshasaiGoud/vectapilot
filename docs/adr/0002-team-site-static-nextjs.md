# ADR 0002 — Team site as a static Next.js export

- **Status:** accepted · 2026-10-09

**Context.** We want a public, animated page introducing the virtual team, live from day one, at
no hosting cost — while practising the frontend stack the product will use later.

**Options.** (a) A plain HTML/CSS page; (b) Next.js on a server platform; (c) Next.js with
`output: 'export'` (static HTML) hosted on GitHub Pages.

**Decision.** (c), in `apps/web/`, with Tailwind, Motion (physics animations) and Radix Dialog
(accessible modal).
- Agent files are read **synchronously during the build**. `"use cache"` is not used: it is a
  runtime cache and unsupported in static exports.
- The template's `cacheComponents` / `partialPrefetching` are **off**: they turn on Partial
  Prerendering, and the build fails with "PPR cannot be enabled in export mode".
- Avatars are generated once by a script (DiceBear "Notionists", CC0) and committed as SVG files.
  Measured: each is ~9–19 KB and the page references them 64 times — inlining would have added
  roughly 0.7 MB to the HTML.
- Scroll reveals use CSS scroll-driven animations as progressive enhancement, so no content is
  hidden while waiting for JavaScript.

**Consequences.** Free hosting and the same stack as the future dashboard. No server features
(server actions, rewrites, request-time data) on the site — acceptable for a static page. The
customer widget and dashboard will need a server later (separate decision).

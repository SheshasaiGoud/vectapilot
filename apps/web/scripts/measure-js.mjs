/**
 * A static page's initial JavaScript, gzipped (M8, "How we will know": the widget page and the
 * team-site home page, before and after PR 4; M0's measure, "initial JS gzipped").
 *
 * The method, so two runs compare: every external script the exported HTML loads
 * (`<script src>`, each file once), read from the export folder and gzipped by Node's zlib at its
 * default level (what `gzip` does with no flag), summed. Scripts inline in the HTML (Next's RSC
 * payload) travel with the HTML, reported next to it. Nothing is downloaded and nothing is
 * written.
 *
 *   node scripts/measure-js.mjs out index.html
 *   node scripts/measure-js.mjs out-ui widget.html
 *
 * Report with the numbers: the commit, the `next` version (printed) and the build command.
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { gzipSync } from "node:zlib";

const [folder, ...pages] = process.argv.slice(2);
if (folder === undefined || pages.length === 0) {
  console.error("usage: node scripts/measure-js.mjs <export folder> <page.html> [...]");
  process.exit(2);
}

const root = path.resolve(folder);
const SCRIPT_SRC = /<script\b[^>]*\bsrc="([^"]+)"/g;
const gz = (bytes) => gzipSync(bytes).length;
const kb = (bytes) => `${(bytes / 1000).toFixed(1)} KB`;

const next = JSON.parse(readFileSync(new URL("../node_modules/next/package.json", import.meta.url), "utf8"));
console.log(`next ${next.version} · export folder ${path.relative(process.cwd(), root) || "."}`);

for (const page of pages) {
  const html = readFileSync(path.join(root, page));
  const sources = [...new Set([...html.toString("utf8").matchAll(SCRIPT_SRC)].map((match) => match[1]))];
  let raw = 0;
  let gzipped = 0;
  for (const source of sources) {
    const file = path.join(root, new URL(source, "http://x/").pathname);
    if (!file.startsWith(root + path.sep)) throw new Error(`script outside the export: ${source}`);
    const bytes = readFileSync(file);
    raw += bytes.length;
    gzipped += gz(bytes);
  }
  console.log(
    `${page}: ${sources.length} scripts, ${raw.toLocaleString("en")} B raw, ` +
      `${gzipped.toLocaleString("en")} B gzipped (${kb(gzipped)}); ` +
      `HTML ${html.length.toLocaleString("en")} B raw, ${gz(html).toLocaleString("en")} B gzipped`,
  );
}

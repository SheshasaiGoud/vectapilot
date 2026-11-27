/**
 * The Pages artefact contains no page that can call an API (M8 PR 4, D2 (i); ADR 0019 decision 4).
 *
 *   node scripts/check-site-out.mjs --site out [--product out-ui]
 *
 * `--site` (the team site, published to GitHub Pages) fails on:
 * - any path whose name starts with `widget` or `staff` (a product page, its payload or its
 *   folder);
 * - any file that names an API path (`/v1/`): the team site calls no API, so no page, script or
 *   payload of it may hold one. Prose that needs to mention a route can say it without the path.
 *
 * `--product` (the API's `ui_dir`, `npm run build:ui`) is the positive control, so the check above
 * can never pass because it looks at nothing: the widget page exists, the scan finds the widget's
 * API prefix in it, and the team site's home page is not there.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: { site: { type: "string" }, product: { type: "string" } },
  strict: true,
});
if (!values.site) {
  console.error("usage: node scripts/check-site-out.mjs --site out [--product out-ui]");
  process.exit(2);
}

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

function filesOf(dir) {
  try {
    return walk(dir);
  } catch {
    console.error(`check-site-out: ${dir}/ does not exist: build it first`);
    process.exit(2);
  }
}

const API_PATH = "/v1/";
const PRODUCT_NAME = /^(widget|staff)/;
const problems = [];

const siteFiles = filesOf(values.site);
for (const file of siteFiles) {
  const path = relative(values.site, file);
  if (path.split(sep).some((part) => PRODUCT_NAME.test(part))) {
    problems.push(`${values.site}/${path}: a product page or asset in the team site`);
  }
  // latin1 reads any byte; the needle is ASCII.
  if (readFileSync(file, "latin1").includes(API_PATH)) {
    problems.push(`${values.site}/${path}: names an API path ("${API_PATH}")`);
  }
}
if (!siteFiles.some((file) => relative(values.site, file) === "index.html")) {
  problems.push(`${values.site}/index.html: missing (is this the team site's build?)`);
}

let productNote = "";
if (values.product) {
  const productFiles = filesOf(values.product);
  const names = productFiles.map((file) => relative(values.product, file));
  if (!names.includes("widget.html")) problems.push(`${values.product}/widget.html: missing`);
  if (names.includes("index.html")) {
    problems.push(`${values.product}/index.html: the team site's home page is in the product build`);
  }
  const callers = productFiles.filter((file) => readFileSync(file, "latin1").includes("/v1/widget/"));
  if (callers.length === 0) {
    problems.push(`${values.product}: no file names "/v1/widget/": the scan would find nothing`);
  }
  productNote = `; ${values.product}/ has widget.html and ${callers.length} file(s) naming /v1/widget/`;
}

if (problems.length > 0) {
  console.error(`check-site-out: ${problems.length} problem(s)`);
  for (const problem of problems) console.error(`  - ${problem}`);
  process.exit(1);
}
console.log(
  `check-site-out: ${values.site}/ (${siteFiles.length} files) has no product page and no API path` +
    productNote,
);

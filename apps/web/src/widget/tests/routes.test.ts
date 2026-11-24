/**
 * S10: the widget calls only `/v1/widget/*`, never `/v1/answer`, `/v1/kb/search` or `/v1/agent`.
 * The route list is pinned, and every widget source file is scanned for any other API path.
 */

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, test } from "node:test";

import { ROUTES, WIDGET_PREFIX, paths } from "../api/routes.ts";

const WIDGET_DIR = fileURLToPath(new URL("..", import.meta.url));

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "tests" ? [] : sources(path);
    return /\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

describe("S10: the widget's API routes", () => {
  test("the route list is exactly the widget's", () => {
    assert.deepEqual(Object.values(ROUTES).sort(), [
      "DELETE /v1/widget/session",
      "GET /v1/widget/handoffs/{id}",
      "GET /v1/widget/{business_id}/config",
      "POST /v1/widget/actions/{id}/confirm",
      "POST /v1/widget/handoffs",
      "POST /v1/widget/messages",
      "POST /v1/widget/sessions",
    ]);
  });

  test("every path the client builds is under /v1/widget/", () => {
    const uuid = "6f1c2a9e-0d4b-4c8e-9a51-2b7e3f4a5c61";
    for (const build of Object.values(paths)) {
      const path = build.length === 0 ? (build as () => string)() : (build as (id: string) => string)(build === paths.config ? "sunrise-clinic" : uuid);
      assert.ok(path.startsWith(WIDGET_PREFIX), path);
    }
  });

  test("no widget source names any other API path", () => {
    const files = sources(WIDGET_DIR);
    assert.ok(files.length > 10, "the scan found the sources");
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      for (const match of text.matchAll(/\/v1\/[a-z_]*/g)) {
        assert.equal(match[0], "/v1/widget", `${file}: ${match[0]}`);
      }
    }
  });

  test("an id that is not of its form never reaches a URL", () => {
    assert.throws(() => paths.config("../staff"));
    assert.throws(() => paths.confirm("1/../../agent"));
    assert.throws(() => paths.handoffStatus("x?y"));
  });
});

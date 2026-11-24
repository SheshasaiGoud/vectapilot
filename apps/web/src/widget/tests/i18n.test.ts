/** D9 (a): the dictionaries are complete, and the check itself catches each kind of gap. */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { describe, test } from "node:test";
import { fileURLToPath } from "node:url";

import { DICTIONARIES, checkDictionaries, chromeFor, format, placeholders } from "../i18n/index.ts";
import { en } from "../i18n/en.ts";
import { SAFETY } from "../i18n/safety.ts";

const others = Object.fromEntries(
  Object.entries(DICTIONARIES)
    .filter(([language]) => language !== "en")
    .map(([language, entry]) => [language, entry.strings]),
);

describe("the dictionaries", () => {
  test("en, hi and te exist; hi and te are labelled unreviewed", () => {
    assert.deepEqual(Object.keys(DICTIONARIES).sort(), ["en", "hi", "te"]);
    assert.equal(DICTIONARIES.en.reviewed, true);
    assert.equal(DICTIONARIES.hi.reviewed, false);
    assert.equal(DICTIONARIES.te.reviewed, false);
  });

  test("every key, the same placeholders, no safety key", () => {
    assert.deepEqual(checkDictionaries(en, others), []);
  });

  test("safety text exists in English only and never as chrome", () => {
    for (const key of Object.keys(SAFETY)) assert.ok(!(key in en), key);
  });

  test("a UI language without a dictionary gets English chrome, marked en", () => {
    assert.deepEqual(chromeFor("ta").lang, "en");
    assert.equal(chromeFor("hi").lang, "hi");
  });

  test("format fills placeholders and leaves unknown ones visible", () => {
    assert.equal(format("{count} of {max}", { count: 3, max: 2000 }), "3 of 2000");
    assert.equal(format("in {seconds} s", {}), "in {seconds} s");
    assert.deepEqual(placeholders("{b} and {a} and {b}"), ["a", "b", "b"]);
  });
});

describe("the completeness check finds each gap (red proofs)", () => {
  const reference = { greeting: "Hello {name}", send: "Send" };
  test("a missing key", () => {
    assert.deepEqual(checkDictionaries(reference, { xx: { greeting: "Hi {name}" } }, []), ['xx: missing "send"']);
  });
  test("an extra key", () => {
    assert.deepEqual(checkDictionaries(reference, { xx: { ...reference, extra: "x" } }, []), ['xx: unexpected key "extra"']);
  });
  test("a placeholder renamed or dropped", () => {
    assert.deepEqual(checkDictionaries(reference, { xx: { greeting: "Hi {nmae}", send: "Go" } }, []), [
      'xx: "greeting" has placeholders [nmae], expected [name]',
    ]);
    assert.equal(checkDictionaries(reference, { xx: { greeting: "Hi", send: "Go" } }, []).length, 1);
  });
  test("an empty or padded value", () => {
    assert.equal(checkDictionaries(reference, { xx: { greeting: "Hi {name}", send: " " } }, []).length, 1);
    assert.equal(checkDictionaries(reference, { xx: { greeting: "Hi {name} ", send: "Go" } }, []).length, 1);
  });
  test("a safety key in the chrome", () => {
    assert.deepEqual(checkDictionaries({ talkToPerson: "Talk" }, {}, ["talkToPerson"]), [
      'reference: "talkToPerson" is a safety key',
    ]);
  });
  // The mutation review's probes: two languages, and placeholders compared as a multiset.
  test("a key missing in two languages is reported for each", () => {
    assert.deepEqual(
      checkDictionaries(reference, { xx: { greeting: "Hi {name}" }, yy: { greeting: "Hey {name}" } }, []),
      ['xx: missing "send"', 'yy: missing "send"'],
    );
  });
  test("a repeated placeholder must stay repeated", () => {
    assert.deepEqual(checkDictionaries({ a: "{n} of {n}" }, { xx: { a: "{n}" } }, []), [
      'xx: "a" has placeholders [n], expected [n,n]',
    ]);
  });
});

describe("the CI gate itself (npm run check:i18n) can fail (mutation review)", () => {
  const WEB_DIR = fileURLToPath(new URL("../../../", import.meta.url));
  const ARGS = ["--disable-warning=MODULE_TYPELESS_PACKAGE_JSON", "scripts/check-i18n.ts"];

  test("it exits 1 on one key missing in te, and 0 on the real dictionaries", () => {
    // A load hook drops one line from te.ts in memory only: no file is written.
    const hook = `
      import { registerHooks } from "node:module";
      registerHooks({
        load(url, context, nextLoad) {
          const result = nextLoad(url, context);
          if (!url.endsWith("/i18n/te.ts")) return result;
          const text = typeof result.source === "string" ? result.source : new TextDecoder().decode(result.source);
          return { ...result, source: text.replace(/^\\s*send: .*\\n/m, ""), shortCircuit: true };
        },
      });`;
    const broken = spawnSync(
      process.execPath,
      ["--import", `data:text/javascript,${encodeURIComponent(hook)}`, ...ARGS],
      { cwd: WEB_DIR, encoding: "utf8" },
    );
    assert.equal(broken.status, 1, broken.stdout + broken.stderr);
    assert.match(broken.stderr, /te: missing "send"/);
    const real = spawnSync(process.execPath, ARGS, { cwd: WEB_DIR, encoding: "utf8" });
    assert.equal(real.status, 0, real.stdout + real.stderr);
  });
});

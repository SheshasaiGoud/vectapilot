import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

/** The product pages' code (M8 PR 4): the widget now, the staff dashboard in PR 5. */
const PRODUCT_FILES = ["src/widget/**/*.{ts,tsx}", "src/app/**/*.product.{ts,tsx}"];

const STORAGE_RULE =
  "S18: the browser keeps the session token in sessionStorage (src/widget/ui/browser.ts) and nothing else";
const POST_RULE = "S18: the widget posts no message (not to its parent, its top window or anyone)";
const LISTENER_RULE = "S18: the widget registers no message listener (a host page cannot drive it)";
const HTML_RULE = "S9: untrusted text is rendered as text, never as HTML";

/** Browser storage, reached through any object (`window`, `self`, `globalThis`, `parent`, ...). */
const STORES = ["localStorage", "sessionStorage", "indexedDB", "caches"];

/** S18's globals; `allow` lifts one (browser.ts opens sessionStorage, and nothing else). */
function restrictedGlobals(allow = []) {
  return [
    "error",
    ...STORES.filter((name) => !allow.includes(name)).map((name) => ({ name, message: STORAGE_RULE })),
    { name: "postMessage", message: POST_RULE },
    { name: "onmessage", message: LISTENER_RULE },
  ];
}

/**
 * S9 and S18 as AST selectors, so the object does not matter: `parent.postMessage(...)`,
 * `window.top?.postMessage(...)`, `globalThis.localStorage`, `self["sessionStorage"]` and a
 * `message` listener named by a template literal are all caught (zara's review: the first rules
 * matched `window.` only). Each was proven to fire on a red sample.
 */
function restrictedSyntax(allow = []) {
  const stores = STORES.filter((name) => !allow.includes(name)).join("|");
  return [
    "error",
    { selector: "CallExpression[callee.property.name='postMessage']", message: POST_RULE },
    { selector: "CallExpression[callee.property.value='postMessage']", message: POST_RULE },
    { selector: `MemberExpression[property.name=/^(${stores})$/]`, message: STORAGE_RULE },
    { selector: `MemberExpression[property.value=/^(${stores})$/]`, message: STORAGE_RULE },
    {
      selector: "CallExpression[callee.property.name='addEventListener'][arguments.0.value='message']",
      message: LISTENER_RULE,
    },
    {
      // A listener named by a template literal or a variable cannot be checked: never allowed.
      selector:
        "CallExpression[callee.property.name='addEventListener'][arguments.0.type!='Literal']",
      message: `${LISTENER_RULE}; name the event with a plain string`,
    },
    { selector: "MemberExpression[property.name=/^(innerHTML|outerHTML)$/]", message: HTML_RULE },
    { selector: "CallExpression[callee.property.name='insertAdjacentHTML']", message: HTML_RULE },
    {
      selector: "CallExpression[callee.object.name='document'][callee.property.name=/^writeln?$/]",
      message: HTML_RULE,
    },
  ];
}

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    // S9 and S18 (docs/plans/m8-pr-checks.md), enforced rather than agreed. Untrusted text
    // (answers, chunks, tool summaries, guidance, business text) is rendered as React text only;
    // nothing reaches the console, Web Storage beyond the one token, a `postMessage`, a message
    // listener or the document title. Type-only imports stay `import type`, because the unit tests
    // run these files with Node's type stripping, which erases nothing else.
    files: PRODUCT_FILES,
    rules: {
      "react/no-danger": "error",
      "react/jsx-no-script-url": "error",
      "no-console": "error",
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      "@typescript-eslint/consistent-type-imports": "error",
      "no-restricted-globals": restrictedGlobals(),
      "no-restricted-properties": [
        "error",
        { object: "window", property: "onmessage", message: LISTENER_RULE },
        { object: "document", property: "title", message: "S18: no message text in the title" },
        { object: "document", property: "cookie", message: "the widget uses no cookie" },
      ],
      "no-restricted-syntax": restrictedSyntax(),
    },
  },
  {
    // The one module that may open sessionStorage (it stores the session token only): only that
    // entry is lifted; every other S9 and S18 rule stays on here (zara's review).
    files: ["src/widget/ui/browser.ts"],
    rules: {
      "no-restricted-globals": restrictedGlobals(["sessionStorage"]),
      "no-restricted-syntax": restrictedSyntax(["sessionStorage"]),
    },
  },
  {
    // Browser tests import `test` from the closed stack (`e2e/support/stack.ts`), whose fixture
    // routes every request and fails on an unanswered one; Playwright's own `test` has no
    // catch-all, so a spec using it could reach the network unnoticed (zara's review).
    files: ["e2e/**/*.spec.ts"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          paths: [
            {
              name: "@playwright/test",
              importNames: ["test"],
              message: "Import `test` from ./support/stack.ts: its catch-all keeps the run closed.",
            },
          ],
        },
      ],
    },
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "out-ui/**",
    "build/**",
    "next-env.d.ts",
  ]),
]);

export default eslintConfig;

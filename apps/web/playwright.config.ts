import { defineConfig } from "@playwright/test";

/**
 * The widget's browser tests (M8 PR 4, step 4b; D8 (a)): the CI job `web e2e`, not required
 * until shesha changes the setting (rule 1). Run `npm run build:ui`, then `npm run test:e2e`.
 *
 * - **Closed and $0:** no web server and no port. Each test's browser context answers every
 *   request itself (`e2e/support/serve.ts`, `stack.ts`): the product build from `out-ui/`, the
 *   API from `fixtures/widget/`. Any other request fails the test.
 * - **The pass rule** ("How we will know"): `retries: 0` (a flaky test is a failing test, never
 *   retried into green), axe with zero violations per state (`e2e/support/checks.ts`).
 * - **Every state at 360×800 and 1280×800** (the two projects), in en, hi and te (the specs),
 *   keyboard only, with `prefers-reduced-motion: reduce`. `e2e/motion.spec.ts` checks the
 *   other preference.
 * - **Chromium only** (its headless shell in CI). Safari and VoiceOver are the manual pass (S15).
 */
export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: process.env.CI ? [["list"], ["github"]] : [["list"]],
  timeout: 30_000,
  expect: { timeout: 5_000 },
  use: {
    browserName: "chromium",
    reducedMotion: "reduce",
    timezoneId: "UTC",
    locale: "en-IN",
    // Nothing may answer from outside the routes: a service worker could.
    serviceWorkers: "block",
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  projects: [
    { name: "phone 360x800", use: { viewport: { width: 360, height: 800 } } },
    { name: "desktop 1280x800", use: { viewport: { width: 1280, height: 800 } } },
  ],
});

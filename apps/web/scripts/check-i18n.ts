/**
 * The widget's i18n completeness check (M8 PR 4, D9 (a)), run in the `web (lint, build)` job:
 * every dictionary has exactly the English keys, with the same `{placeholders}`, no empty value,
 * and no safety key (safety text is English only). `npm run check:i18n`; exit 1 on any problem.
 */

import { en } from "../src/widget/i18n/en.ts";
import { DICTIONARIES, checkDictionaries } from "../src/widget/i18n/index.ts";

const others = Object.fromEntries(
  Object.entries(DICTIONARIES)
    .filter(([language]) => language !== "en")
    .map(([language, entry]) => [language, entry.strings]),
);
const problems = checkDictionaries(en, others);

if (problems.length > 0) {
  console.error(`i18n: ${problems.length} problem(s)`);
  for (const problem of problems) console.error(`  - ${problem}`);
  process.exit(1);
}

const unreviewed = Object.entries(DICTIONARIES)
  .filter(([, entry]) => !entry.reviewed)
  .map(([language]) => language);
console.log(
  `i18n: ${Object.keys(en).length} keys in ${Object.keys(DICTIONARIES).join(", ")}: complete` +
    (unreviewed.length > 0 ? ` (not reviewed by a native speaker: ${unreviewed.join(", ")}, D9)` : ""),
);

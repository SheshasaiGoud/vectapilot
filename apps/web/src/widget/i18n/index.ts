/**
 * The widget's dictionaries (D9 (a)) and the completeness check run in CI
 * (`scripts/check-i18n.ts`, `npm run check:i18n`).
 *
 * - Chrome: `en` (reference), `hi`, `te`. A UI language without a dictionary gets English chrome,
 *   marked `lang="en"`; business text keeps its own `lang` either way.
 * - Safety text: `safety.ts`, English only, `lang="en"`, never in a translated dictionary.
 */

import { en } from "./en.ts";
import type { ChromeKey, Dictionary } from "./en.ts";
import { hi } from "./hi.ts";
import { SAFETY } from "./safety.ts";
import { te } from "./te.ts";

export interface DictionaryEntry {
  strings: Dictionary;
  /** False until a named native speaker signs off (D9 (a)); the UI then says so. */
  reviewed: boolean;
}

export const DICTIONARIES: Readonly<Record<string, DictionaryEntry>> = {
  en: { strings: en, reviewed: true },
  hi: { strings: hi, reviewed: false },
  te: { strings: te, reviewed: false },
};

export interface Chrome {
  /** The language the chrome is actually in (the UI language, or `en` as the fallback). */
  lang: string;
  strings: Dictionary;
  reviewed: boolean;
}

export function chromeFor(uiLanguage: string): Chrome {
  const entry = DICTIONARIES[uiLanguage];
  return entry === undefined
    ? { lang: "en", strings: en, reviewed: true }
    : { lang: uiLanguage, strings: entry.strings, reviewed: entry.reviewed };
}

const PLACEHOLDER = /\{([a-z]+)\}/g;

/** Fills `{name}` placeholders. Values are inserted as text; React renders the result as text. */
export function format(template: string, values: Readonly<Record<string, string | number>> = {}) {
  return template.replace(PLACEHOLDER, (whole, name: string) =>
    Object.hasOwn(values, name) ? String(values[name]) : whole,
  );
}

export function placeholders(template: string): string[] {
  return [...template.matchAll(PLACEHOLDER)].map((match) => match[1]).sort();
}

/**
 * Every problem of `dictionaries` against `reference`: a missing or extra key, an empty or
 * untrimmed value, different placeholders, or a key that is also a safety key (safety text is
 * never translated). Empty: complete.
 */
export function checkDictionaries(
  reference: Readonly<Record<string, string>>,
  dictionaries: Readonly<Record<string, Readonly<Record<string, string>>>>,
  safetyKeys: readonly string[] = Object.keys(SAFETY),
): string[] {
  const problems: string[] = [];
  const keys = Object.keys(reference);
  for (const key of keys) {
    if (safetyKeys.includes(key)) problems.push(`reference: "${key}" is a safety key`);
  }
  for (const [language, dictionary] of Object.entries(dictionaries)) {
    for (const key of keys) {
      const value = dictionary[key];
      if (value === undefined) {
        problems.push(`${language}: missing "${key}"`);
        continue;
      }
      if (value.trim() === "" || value !== value.trim()) {
        problems.push(`${language}: "${key}" is empty or has outer whitespace`);
      }
      const want = placeholders(reference[key]).join(",");
      const got = placeholders(value).join(",");
      if (want !== got) {
        problems.push(`${language}: "${key}" has placeholders [${got}], expected [${want}]`);
      }
    }
    for (const key of Object.keys(dictionary)) {
      if (!keys.includes(key)) problems.push(`${language}: unexpected key "${key}"`);
    }
  }
  return problems;
}

export type { ChromeKey, Dictionary };

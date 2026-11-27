/**
 * What the widget page reads from its URL (S18): the business from the path the API serves it
 * at, `/widget/{business_id}` (static export: no request-time routing, so the id is read in the
 * client), and from the query only `lang`. Nothing else may be passed: host data in the iframe's
 * `src` would land in the API's access log.
 */

const WIDGET_PATH = /^\/widget\/([a-z0-9]+(?:-[a-z0-9]+)*)\/?$/;
const LANGUAGE_TAG = /^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$/;
const BUSINESS_ID_LENGTH = { min: 3, max: 40 };
const LANGUAGE_TAG_MAX = 35;

export interface WidgetLocation {
  businessId: string | null;
  lang: string | null;
}

export function readLocation(pathname: string, search: string): WidgetLocation {
  const match = WIDGET_PATH.exec(pathname);
  const id = match?.[1] ?? null;
  const businessId =
    id !== null && id.length >= BUSINESS_ID_LENGTH.min && id.length <= BUSINESS_ID_LENGTH.max
      ? id
      : null;
  const lang = new URLSearchParams(search).get("lang");
  const validLang =
    lang !== null && lang.length <= LANGUAGE_TAG_MAX && LANGUAGE_TAG.test(lang) ? lang : null;
  return { businessId, lang: validLang };
}

/** The UI language: the query's `lang` when the business offers it, else its first language. */
export function chooseLanguage(requested: string | null, offered: readonly string[]): string {
  return requested !== null && offered.includes(requested) ? requested : offered[0];
}

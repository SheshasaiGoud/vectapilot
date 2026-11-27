/**
 * The widget as a keyboard user meets it (M8 PR 4, step 4b): locators by role and by the
 * widget's own dictionaries, so one test runs in every UI language, and actions by keyboard only.
 *
 * The languages run against the sample businesses that offer them (`data/widget/`): en and hi
 * at sunrise-clinic, te at paws-and-co (D9). Customer text comes from that business's own
 * configured quick replies, so no test invents text in a language.
 */

import type { Locator, Page } from "@playwright/test";

import { chromeFor } from "../../src/widget/i18n/index.ts";
import type { Dictionary } from "../../src/widget/i18n/index.ts";
import { SAFETY } from "../../src/widget/i18n/safety.ts";
import { press, tabTo } from "./checks.ts";
import { UI_ORIGIN } from "./serve.ts";
import { fixture } from "./stack.ts";
import type { FixtureCase, Reply, Stack } from "./stack.ts";

export interface Language {
  lang: "en" | "hi" | "te";
  business: string;
}

export const LANGUAGES: readonly Language[] = [
  { lang: "en", business: "sunrise-clinic" },
  { lang: "hi", business: "sunrise-clinic" },
  { lang: "te", business: "paws-and-co" },
];

export interface Texts {
  language: string;
  greeting: string;
  contact_fallback: string[];
  quick_replies: string[];
  handoff_follow_up?: string | null;
  urgent_follow_up?: string | null;
}

export interface ConfigBody {
  business_id: string;
  display_name: string;
  languages: string[];
  texts: Texts[];
  guidance: string[];
}

/** The config case with both follow-up texts, where the fixtures have one (both sample
 * businesses: `sunrise-clinic-follow-ups`, `paws-and-co-follow-ups`). */
export function followUpConfig(business: string): string {
  return business === "sunrise-clinic" || business === "paws-and-co"
    ? `${business}-follow-ups`
    : business;
}

/** `{name}`-style placeholders as a pattern: `format`'s output with any value. */
export function templatePattern(template: string): RegExp {
  const escaped = template.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(escaped.replace(/\\\{\w+\\\}/g, ".+"));
}

export class Widget {
  readonly page: Page;
  readonly stack: Stack;
  readonly lang: Language["lang"];
  readonly business: string;
  readonly strings: Dictionary;
  config: ConfigBody;

  constructor(page: Page, stack: Stack, { lang, business }: Language, configCase = business) {
    this.page = page;
    this.stack = stack;
    this.lang = lang;
    this.business = business;
    this.strings = chromeFor(lang).strings;
    this.config = fixture("config", configCase).body as ConfigBody;
  }

  get texts(): Texts {
    const texts = this.config.texts.find((entry) => entry.language === this.lang);
    if (texts === undefined) throw new Error(`${this.business} has no ${this.lang} texts`);
    return texts;
  }

  /** The customer's message: this business's first quick reply in this language. */
  get message(): string {
    return this.texts.quick_replies[0];
  }

  get guidance(): string[] {
    return this.config.guidance;
  }

  get url(): string {
    const query = this.lang === "en" ? "" : `?lang=${this.lang}`;
    return `${UI_ORIGIN}/widget/${this.business}${query}`;
  }

  // ── Locators ────────────────────────────────────────────────────────────────────────────────

  get box(): Locator {
    return this.page.locator("#vp-message");
  }
  get sendButton(): Locator {
    return this.page.getByRole("button", { name: this.strings.send, exact: true });
  }
  get talkButton(): Locator {
    return this.page.getByRole("button", { name: SAFETY.talkToPerson, exact: true });
  }
  get endButton(): Locator {
    return this.page.getByRole("button", { name: this.strings.endChat, exact: true });
  }
  get status(): Locator {
    return this.page.locator("main div[role='status']");
  }
  get contact(): Locator {
    return this.page.getByRole("heading", {
      name: this.strings.contactHeading.replace("{name}", this.config.display_name),
    });
  }
  get log(): Locator {
    return this.page.getByRole("log");
  }
  text(value: string): Locator {
    return this.page.getByText(value, { exact: true });
  }

  // ── Actions (keyboard only) ─────────────────────────────────────────────────────────────────

  /** Stubs the config route, and session creation unless `session` is given. */
  prepare({ config, session }: { config?: Reply[]; session?: Reply[] } = {}): void {
    const body = this.config;
    this.stack.business = { businessId: this.business, lang: this.lang, guidance: body.guidance };
    const configReplies: Reply[] = config ?? [{ status: 200, model: "WidgetConfigResponse", body }];
    this.stack.on(`GET /v1/widget/${this.business}/config`, ...configReplies);
    this.stack.on("POST /v1/widget/sessions", ...(session ?? [fixture("sessions", "created")]));
  }

  /** `prepare`, then load the widget page top level. */
  async open(stubs: { config?: Reply[]; session?: Reply[] } = {}): Promise<void> {
    this.prepare(stubs);
    await this.page.goto(this.url);
  }

  async ready(): Promise<void> {
    await this.page.getByRole("heading", { level: 1, name: this.config.display_name }).waitFor();
  }

  /** Tab to the message box, type, press Enter. */
  async send(text: string = this.message): Promise<void> {
    await tabTo(this.page, this.box);
    await this.page.keyboard.type(text);
    await this.page.keyboard.press("Enter");
  }

  async press(target: Locator): Promise<void> {
    await press(this.page, target);
  }

  /** The keys in `sessionStorage` (S18: the session token's key and nothing else). */
  async storageKeys(): Promise<string[]> {
    return this.page.evaluate(() => Object.keys(window.sessionStorage));
  }
}

export function messageReply(name: string): FixtureCase {
  return fixture("messages", name);
}

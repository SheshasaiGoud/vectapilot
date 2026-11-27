/**
 * Test doubles for the widget's unit tests (`npm run test:unit`): a scripted API, manual timers, a
 * storage spy and the fixtures in `apps/web/fixtures/widget/`. No network, no browser, $0.
 */

import { readFileSync } from "node:fs";

import type { FetchLike, Timers } from "../api/client.ts";
import { WidgetController } from "../core/controller.ts";
import type { StorageLike } from "../core/storage.ts";
import { tokenStore } from "../core/storage.ts";
import type { WidgetState } from "../core/state.ts";
import { deriveView } from "../core/view.ts";
import type { WidgetView } from "../core/view.ts";

// ── Fixtures ────────────────────────────────────────────────────────────────────────────────────

export interface FixtureCase {
  status: number;
  model: string;
  body: unknown;
  headers?: Record<string, string>;
  text?: string;
}

const cache = new Map<string, Record<string, FixtureCase>>();

/** Every case of `fixtures/widget/<file>.json`. */
export function allCases(file: string): Record<string, FixtureCase> {
  let cases = cache.get(file);
  if (cases === undefined) {
    const url = new URL(`../../../fixtures/widget/${file}.json`, import.meta.url);
    cases = (JSON.parse(readFileSync(url, "utf8")) as { cases: Record<string, FixtureCase> }).cases;
    cache.set(file, cases);
  }
  return cases;
}

export function fixture(file: string, name: string): FixtureCase {
  const found = allCases(file)[name];
  if (found === undefined) throw new Error(`no fixture ${file}:${name}`);
  return structuredClone(found);
}

// ── Manual timers ───────────────────────────────────────────────────────────────────────────────

interface Scheduled {
  id: number;
  at: number;
  every: number | null;
  callback: () => void;
}

export class ManualTimers implements Timers {
  now = Date.UTC(2026, 10, 24, 12, 0, 0);
  #next = 1;
  #queue: Scheduled[] = [];

  setTimeout(callback: () => void, ms: number): unknown {
    const id = this.#next++;
    this.#queue.push({ id, at: this.now + ms, every: null, callback });
    return id;
  }

  setInterval(callback: () => void, ms: number): unknown {
    const id = this.#next++;
    this.#queue.push({ id, at: this.now + ms, every: ms, callback });
    return id;
  }

  clearTimeout(handle: unknown): void {
    this.#queue = this.#queue.filter((item) => item.id !== handle);
  }

  clearInterval(handle: unknown): void {
    this.clearTimeout(handle);
  }

  pending(): number {
    return this.#queue.length;
  }

  /** Moves the clock forward, running every callback that falls due, in order. */
  async advance(ms: number): Promise<void> {
    const end = this.now + ms;
    for (;;) {
      this.#queue.sort((a, b) => a.at - b.at);
      const due = this.#queue[0];
      if (due === undefined || due.at > end) break;
      this.now = due.at;
      if (due.every === null) this.#queue.shift();
      else due.at += due.every;
      due.callback();
      await settle();
    }
    this.now = end;
    await settle();
  }
}

/** Lets every pending promise chain run (fetch, `response.text()`, the controller's awaits). */
export async function settle(): Promise<void> {
  for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

// ── A scripted API ──────────────────────────────────────────────────────────────────────────────

export interface Call {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
  init: RequestInit;
}

/** A reply the test releases when it chooses (a request still in flight meanwhile). */
export class Held {
  #release: ((reply: FixtureCase) => void) | null = null;
  #pending: FixtureCase | null = null;

  /** Answers the held request now (or as soon as it arrives). */
  release(reply: FixtureCase): void {
    if (this.#release !== null) this.#release(reply);
    else this.#pending = reply;
  }

  wait(): Promise<FixtureCase> {
    if (this.#pending !== null) return Promise.resolve(this.#pending);
    return new Promise((resolve) => (this.#release = resolve));
  }
}

export type Reply = FixtureCase | "hang" | "network" | Held;

function toResponse(reply: FixtureCase): Response {
  const text =
    reply.text ?? (reply.body === undefined || reply.body === null ? "" : JSON.stringify(reply.body));
  const responseBody = reply.status === 204 || text === "" ? null : text;
  return new Response(responseBody, { status: reply.status, headers: reply.headers });
}

export class FakeApi {
  readonly calls: Call[] = [];
  readonly #routes = new Map<string, Reply[]>();
  readonly #defaults = new Map<string, Reply>();

  /** The next replies for `METHOD path`, in order, one per call; once they are used up, the
   * `always` reply, else the call fails as an unexpected request. */
  on(route: string, ...replies: Reply[]): this {
    this.#routes.set(route, [...(this.#routes.get(route) ?? []), ...replies]);
    return this;
  }

  /** A reply for every call to `route` once the queue is empty. */
  always(route: string, reply: Reply): this {
    this.#defaults.set(route, reply);
    return this;
  }

  count(route: string): number {
    return this.calls.filter((call) => `${call.method} ${call.path}` === route).length;
  }

  fetch: FetchLike = (input, init) => {
    const method = init.method ?? "GET";
    const headers = Object.fromEntries(new Headers(init.headers).entries());
    const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
    this.calls.push({ method, path: input, headers, body, init });
    const route = `${method} ${input}`;
    const queue = this.#routes.get(route);
    const reply = queue !== undefined && queue.length > 0 ? queue.shift()! : this.#defaults.get(route);
    if (reply === undefined) {
      return Promise.reject(new Error(`unexpected request: ${route}`));
    }
    if (reply === "network") return Promise.reject(new TypeError("fetch failed"));
    if (reply === "hang" || reply instanceof Held) {
      return new Promise((resolve, reject) => {
        init.signal?.addEventListener("abort", () =>
          reject(new DOMException("The operation was aborted.", "AbortError")),
        );
        if (reply instanceof Held) void reply.wait().then((held) => resolve(toResponse(held)));
      });
    }
    return Promise.resolve(toResponse(reply));
  };
}

// ── A storage spy ───────────────────────────────────────────────────────────────────────────────

export class SpyStorage implements StorageLike {
  readonly writes: [string, string][] = [];
  readonly removes: string[] = [];
  readonly reads: string[] = [];
  readonly #items = new Map<string, string>();

  getItem(key: string): string | null {
    this.reads.push(key);
    return this.#items.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.writes.push([key, value]);
    this.#items.set(key, value);
  }

  removeItem(key: string): void {
    this.removes.push(key);
    this.#items.delete(key);
  }

  keys(): string[] {
    return [...this.#items.keys()];
  }
}

// ── A widget under test ─────────────────────────────────────────────────────────────────────────

export const SUNRISE = "sunrise-clinic";
export const ROUTE = {
  config: (id: string) => `GET /v1/widget/${id}/config`,
  session: "POST /v1/widget/sessions",
  end: "DELETE /v1/widget/session",
  message: "POST /v1/widget/messages",
  handoff: "POST /v1/widget/handoffs",
  status: (id: string) => `GET /v1/widget/handoffs/${id}`,
  confirm: (id: string) => `POST /v1/widget/actions/${id}/confirm`,
} as const;

export interface Harness {
  widget: WidgetController;
  api: FakeApi;
  timers: ManualTimers;
  storage: SpyStorage;
  state(): WidgetState;
  view(): WidgetView;
  /** Types `text`, presses Send and lets the answer arrive. */
  say(text: string): Promise<void>;
}

export async function openWidget(
  options: {
    businessId?: string;
    lang?: string;
    config?: FixtureCase;
    api?: FakeApi;
    storage?: SpyStorage;
  } = {},
): Promise<Harness> {
  const businessId = options.businessId ?? SUNRISE;
  const api = options.api ?? new FakeApi();
  const storage = options.storage ?? new SpyStorage();
  const timers = new ManualTimers();
  api.on(ROUTE.config(businessId), options.config ?? fixture("config", businessId));
  const widget = new WidgetController({
    fetch: api.fetch,
    timers,
    tokens: tokenStore(storage),
    now: () => timers.now,
    location: {
      pathname: `/widget/${businessId}`,
      search: options.lang === undefined ? "" : `?lang=${options.lang}`,
    },
  });
  widget.start();
  await settle();
  const harness: Harness = {
    widget,
    api,
    timers,
    storage,
    state: () => widget.getSnapshot(),
    view: () => deriveView(widget.getSnapshot()),
    async say(text: string) {
      widget.setDraft(text);
      const sent = widget.send();
      await settle();
      await sent;
    },
  };
  return harness;
}

/** The texts of every line a view shows, in order (for "is X shown" assertions). */
export function shownTexts(view: WidgetView): string[] {
  const texts: string[] = [];
  const walk = (value: unknown): void => {
    if (Array.isArray(value)) value.forEach(walk);
    else if (value !== null && typeof value === "object") {
      const record = value as Record<string, unknown>;
      if (typeof record.text === "string" && "lang" in record) texts.push(record.text);
      else Object.values(record).forEach(walk);
    }
  };
  walk(view);
  return texts;
}

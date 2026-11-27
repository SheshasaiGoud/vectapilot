/**
 * One HTTP call to the widget API, which never throws: every result is a response, a client
 * timeout, a network error or an abort (the widget was closed). The callers turn each into a
 * state; nothing here retries (the widget never re-sends a message by itself, S16, D15).
 *
 * Request options, each on purpose:
 * - `credentials: "omit"`: the widget authenticates with its bearer token only. The staff cookie
 *   lives on the same origin in M8 (D12 (b)); the widget page never sends it.
 * - `mode` stays the default (`"cors"`; the paths are same-origin anyway): the page is served with
 *   `Referrer-Policy: no-referrer` (S9), and under that policy a `mode: "same-origin"` POST sends
 *   `Origin: null` (Fetch standard, "append a request `Origin` header"), which session creation's
 *   same-origin check refuses (PR 1, `require_same_origin`).
 * - `redirect: "error"`: a redirect is never followed, so the bearer token cannot be carried to
 *   another URL.
 * - `cache: "no-store"`: answers, replies and tokens never sit in the HTTP cache (S9).
 */

export type FetchLike = (input: string, init: RequestInit) => Promise<Response>;

export interface Timers {
  setTimeout(callback: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
  setInterval(callback: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
}

export type HttpResult =
  | { kind: "response"; status: number; retryAfterSeconds: number | null; body: unknown }
  | { kind: "timeout" }
  | { kind: "network" }
  | { kind: "aborted" };

export interface RequestOptions {
  method: "GET" | "POST" | "DELETE";
  path: string;
  timeoutMs: number;
  token?: string | null;
  body?: unknown;
  /** Aborts the call when the widget is closed; the result is then `aborted` and ignored. */
  signal?: AbortSignal;
}

const DELAY_SECONDS = /^\d{1,9}$/;

/** `Retry-After` as whole seconds (the API sends seconds, `guards/limits.py`); a date is ignored. */
export function parseRetryAfter(value: string | null): number | null {
  if (value === null) return null;
  const trimmed = value.trim();
  return DELAY_SECONDS.test(trimmed) ? Number(trimmed) : null;
}

export async function request(
  fetchImpl: FetchLike,
  timers: Timers,
  options: RequestOptions,
): Promise<HttpResult> {
  const controller = new AbortController();
  let timedOut = false;
  const outer = options.signal;
  const forward = () => controller.abort();
  if (outer?.aborted) return { kind: "aborted" };
  outer?.addEventListener("abort", forward, { once: true });
  const timer = timers.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, options.timeoutMs);

  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.token) headers.Authorization = `Bearer ${options.token}`;
  const init: RequestInit = {
    method: options.method,
    headers,
    signal: controller.signal,
    credentials: "omit",
    cache: "no-store",
    redirect: "error",
  };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }

  try {
    const response = await fetchImpl(options.path, init);
    const text = await response.text(); // still under the same timeout
    let body: unknown = undefined;
    if (text !== "") {
      try {
        body = JSON.parse(text);
      } catch {
        body = undefined; // not JSON: the caller treats it as a body it cannot read
      }
    }
    return {
      kind: "response",
      status: response.status,
      retryAfterSeconds: parseRetryAfter(response.headers.get("retry-after")),
      body,
    };
  } catch {
    if (outer?.aborted) return { kind: "aborted" };
    return timedOut ? { kind: "timeout" } : { kind: "network" };
  } finally {
    timers.clearTimeout(timer);
    outer?.removeEventListener("abort", forward);
  }
}

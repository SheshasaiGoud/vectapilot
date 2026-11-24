/**
 * The browser's side of the controller's dependencies: the one module that touches
 * `sessionStorage` (ESLint allows it here only, S18), the real `fetch`, timers and location.
 */

import type { WidgetDeps } from "../core/controller.ts";
import { tokenStore } from "../core/storage.ts";
import type { StorageLike } from "../core/storage.ts";

function sessionStorageOrNull(): StorageLike | null {
  try {
    return window.sessionStorage; // may throw: storage blocked, or a sandboxed frame
  } catch {
    return null;
  }
}

export function browserDeps(): WidgetDeps {
  return {
    fetch: (input, init) => window.fetch(input, init),
    timers: {
      setTimeout: (callback, ms) => window.setTimeout(callback, ms),
      clearTimeout: (handle) => window.clearTimeout(handle as number),
      setInterval: (callback, ms) => window.setInterval(callback, ms),
      clearInterval: (handle) => window.clearInterval(handle as number),
    },
    tokens: tokenStore(sessionStorageOrNull()),
    now: () => Date.now(),
    location: { pathname: window.location.pathname, search: window.location.search },
  };
}

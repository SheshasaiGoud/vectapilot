/**
 * What the browser keeps (S18): the session token, in `sessionStorage`, under one key per
 * business, and nothing else. The transcript and every confirm token live in memory only.
 *
 * One key per business because one origin serves every business's widget (D2 (a)): two widgets
 * in one tab must never share a session. A stored token is used only when it names this
 * business (`<business_id>.<secret>`, S3). Storage that throws (blocked, full, a sandboxed frame)
 * degrades to memory only: the chat still works for this page view.
 */

/** The subset of the Web Storage API the widget uses (a test passes a spy). */
export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface TokenStore {
  read(businessId: string): string | null;
  write(businessId: string, token: string): void;
  clear(businessId: string): void;
}

export const TOKEN_KEY_PREFIX = "vectapilot.widget.session.";

export function tokenKey(businessId: string): string {
  return `${TOKEN_KEY_PREFIX}${businessId}`;
}

export function tokenStore(storage: StorageLike | null): TokenStore {
  return {
    read(businessId) {
      try {
        const token = storage?.getItem(tokenKey(businessId)) ?? null;
        return token !== null && token.startsWith(`${businessId}.`) ? token : null;
      } catch {
        return null;
      }
    },
    write(businessId, token) {
      try {
        storage?.setItem(tokenKey(businessId), token);
      } catch {
        // Memory only for this page view.
      }
    },
    clear(businessId) {
      try {
        storage?.removeItem(tokenKey(businessId));
      } catch {
        // Nothing stored, or storage is unavailable.
      }
    },
  };
}

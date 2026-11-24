/**
 * Every API route the widget may call (S10). The widget never calls the answer or the KB search
 * route (their guards are off until M7 PR 10) or the agent route (the harness path): a test pins
 * this list and scans the widget's sources for any API path outside the widget's prefix.
 *
 * Paths are relative: the API serves the widget page from its own origin (D2 (a)), so every call
 * is same-origin and CORS stays off.
 */

export const WIDGET_PREFIX = "/v1/widget/";

export const ROUTES = {
  config: "GET /v1/widget/{business_id}/config",
  createSession: "POST /v1/widget/sessions",
  endSession: "DELETE /v1/widget/session",
  message: "POST /v1/widget/messages",
  handoff: "POST /v1/widget/handoffs",
  handoffStatus: "GET /v1/widget/handoffs/{id}",
  confirm: "POST /v1/widget/actions/{id}/confirm",
} as const;

export type RouteName = keyof typeof ROUTES;

const SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function segment(value: string, pattern: RegExp): string {
  // Ids come from the page's path or from an API body: never trusted into a URL unchecked.
  if (!pattern.test(value)) {
    throw new Error("an id in a widget route has an unexpected form");
  }
  return value;
}

export const paths = {
  config: (businessId: string) => `${WIDGET_PREFIX}${segment(businessId, SLUG)}/config`,
  createSession: () => `${WIDGET_PREFIX}sessions`,
  endSession: () => `${WIDGET_PREFIX}session`,
  message: () => `${WIDGET_PREFIX}messages`,
  handoff: () => `${WIDGET_PREFIX}handoffs`,
  handoffStatus: (handoffId: string) => `${WIDGET_PREFIX}handoffs/${segment(handoffId, UUID)}`,
  confirm: (actionId: string) => `${WIDGET_PREFIX}actions/${segment(actionId, UUID)}/confirm`,
} as const satisfies Record<RouteName, (...ids: string[]) => string>;

export { SLUG as BUSINESS_ID_PATTERN, UUID as UUID_PATTERN };

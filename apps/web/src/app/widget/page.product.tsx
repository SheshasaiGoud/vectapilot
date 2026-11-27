import { WidgetRoot } from "@/widget/ui/WidgetRoot.tsx";

/**
 * The customer chat widget (M8 PR 4; ADR 0019 decisions 4 and 5). Built only for the API's
 * `ui_dir` (`npm run build:ui`), never for GitHub Pages. The API serves this one page at
 * `/widget/{business_id}` (PR 2) with its CSP and per-business `frame-ancestors`; the widget reads
 * the business id from that path and only `lang` from the query (S18).
 */
export default function WidgetPage() {
  return <WidgetRoot />;
}

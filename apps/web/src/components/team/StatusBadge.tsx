import type { Status } from "@/lib/types";

// Full class names (not built from strings) so Tailwind can find them.
const STYLES = {
  active: { badge: "bg-emerald-400/10 text-emerald-300 ring-emerald-400/30", dot: "bg-emerald-400" },
  next: { badge: "bg-amber-400/10 text-amber-200 ring-amber-400/30", dot: "bg-amber-400" },
  standby: { badge: "bg-white/5 text-mist-300 ring-white/15", dot: "bg-mist-400" },
} as const;

export function StatusBadge({ status }: { status: Status }) {
  const style = STYLES[status.kind];
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium whitespace-nowrap ring-1 ${style.badge}`}
    >
      <span className="relative flex size-2" aria-hidden>
        {status.kind === "active" && (
          <span className={`absolute inline-flex size-full animate-ping rounded-full opacity-60 ${style.dot}`} />
        )}
        <span className={`relative inline-flex size-2 rounded-full ${style.dot}`} />
      </span>
      {status.label}
    </span>
  );
}

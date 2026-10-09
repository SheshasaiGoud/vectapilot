import { SectionHeading } from "@/components/sections/SectionHeading";
import { OwnerAvatars } from "@/components/team/OwnerAvatars";
import type { MilestoneView } from "@/lib/types";

export function Roadmap({ roadmap }: { roadmap: MilestoneView[] }) {
  return (
    <section id="roadmap" aria-labelledby="roadmap-title" className="mx-auto max-w-6xl px-4 py-24 sm:px-6">
      <SectionHeading id="roadmap-title" eyebrow="Roadmap" title="From foundations to a real pilot.">
        Twelve milestones, each ending with a demo, tests and measured results. Statuses on this page are
        derived from the roadmap file — move a milestone forward and the whole crew updates.
      </SectionHeading>

      <ol className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {roadmap.map((milestone) => (
          <li key={milestone.id} className="reveal">
            <MilestoneCard milestone={milestone} />
          </li>
        ))}
      </ol>
    </section>
  );
}

function MilestoneCard({ milestone }: { milestone: MilestoneView }) {
  const current = milestone.state === "current";
  const body = (
    <div className={`flex h-full flex-col rounded-[15px] p-4 ${current ? "bg-ink-900" : ""}`}>
      <div className="flex items-center justify-between gap-2">
        <span className={`font-mono text-sm font-bold ${current ? "text-indigo-300" : "text-mist-400"}`}>
          {milestone.id}
        </span>
        <StateChip state={milestone.state} />
      </div>
      <h3 className={`mt-2 font-display font-semibold ${current ? "text-mist-50" : "text-mist-200"}`}>
        {milestone.title}
      </h3>
      <p className="mt-1 text-sm leading-relaxed text-mist-400">{milestone.summary}</p>
      <div className="mt-auto pt-4">
        <OwnerAvatars owners={milestone.owners} />
      </div>
    </div>
  );

  if (current) {
    // Animated gradient border marks the milestone in progress.
    return (
      <div className="h-full animate-shimmer rounded-2xl bg-[linear-gradient(90deg,#818cf8,#f0abfc,#67e8f9,#818cf8)] bg-[length:200%_auto] p-px shadow-[0_20px_50px_-20px_#818cf8]">
        {body}
      </div>
    );
  }
  return <div className="h-full rounded-2xl bg-ink-900/50 ring-1 ring-white/10">{body}</div>;
}

function StateChip({ state }: { state: MilestoneView["state"] }) {
  if (state === "current") {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-indigo-400/15 px-2 py-0.5 text-[11px] font-medium text-indigo-200 ring-1 ring-indigo-400/30">
        <span className="size-1.5 animate-pulse rounded-full bg-indigo-300" aria-hidden />
        In progress
      </span>
    );
  }
  if (state === "done") {
    return (
      <span className="rounded-full bg-emerald-400/10 px-2 py-0.5 text-[11px] font-medium text-emerald-300 ring-1 ring-emerald-400/30">
        ✓ Done
      </span>
    );
  }
  return <span className="text-[11px] text-mist-400">Planned</span>;
}

import { SectionHeading } from "@/components/sections/SectionHeading";
import { TeamGrid } from "@/components/team/TeamGrid";
import type { Teammate } from "@/lib/types";

export function TeamSection({ team }: { team: Teammate[] }) {
  return (
    <section id="team" aria-labelledby="team-title" className="mx-auto max-w-6xl px-4 py-24 sm:px-6">
      <SectionHeading id="team-title" eyebrow="The team" title="Eight specialists. One shared rulebook.">
        Each teammate is a real Claude Code subagent defined in{" "}
        <code className="rounded bg-white/5 px-1.5 py-0.5 font-mono text-sm text-mist-50">.claude/agents/</code>. This
        page is built from those files, so it can never drift from the real team. Click anyone to read their brief.
      </SectionHeading>

      <div className="mt-12">
        <TeamGrid team={team} />
      </div>

      <ul className="mt-8 flex flex-wrap gap-x-6 gap-y-2 text-xs text-mist-400" aria-label="Status legend">
        <li className="flex items-center gap-2">
          <span className="size-2 rounded-full bg-emerald-400" aria-hidden /> On the current milestone
        </li>
        <li className="flex items-center gap-2">
          <span className="size-2 rounded-full bg-amber-400" aria-hidden /> Up within two milestones
        </li>
        <li className="flex items-center gap-2">
          <span aria-hidden>💤</span> Standby until their milestone
        </li>
      </ul>
    </section>
  );
}

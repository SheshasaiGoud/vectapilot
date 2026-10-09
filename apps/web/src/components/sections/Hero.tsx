import { GitHubIcon } from "@/components/sections/Nav";
import { TeamOrbit } from "@/components/team/TeamOrbit";
import { currentMilestone } from "@/data/roadmap";
import { REPO_URL } from "@/lib/team";
import type { HumanLead, Teammate } from "@/lib/types";

export function Hero({ team, human }: { team: Teammate[]; human: HumanLead }) {
  const stats = [
    { value: team.length, label: "AI teammates" },
    { value: 1, label: "human lead" },
    { value: 0, label: "unmeasured claims" },
  ];

  return (
    <section aria-labelledby="hero-title" className="relative isolate overflow-hidden">
      <Aurora />
      <div className="mx-auto grid max-w-6xl items-center gap-12 px-4 pb-20 pt-28 sm:px-6 lg:grid-cols-[1.05fr_1fr] lg:gap-8 lg:pt-36">
        <div>
          <p className="inline-flex animate-rise items-center gap-2 rounded-full bg-white/5 px-3 py-1 text-xs text-mist-200 ring-1 ring-white/10">
            <span className="relative flex size-2" aria-hidden>
              <span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-400 opacity-60" />
              <span className="relative inline-flex size-2 rounded-full bg-emerald-400" />
            </span>
            Built in public · Milestone {currentMilestone} in progress
          </p>

          <h1
            id="hero-title"
            className="mt-6 animate-rise font-display text-5xl font-bold leading-[1.05] tracking-tight [animation-delay:80ms] sm:text-6xl"
          >
            Meet the crew building <span className="text-shimmer">VectaPilot</span>
          </h1>

          <p className="mt-6 max-w-xl animate-rise text-lg leading-relaxed text-mist-300 [animation-delay:160ms]">
            Eight AI teammates and one human lead, building a supervised AI support copilot: answers
            grounded in a business&apos;s own documents, actions that ask before they act, and evals that
            block regressions.
          </p>

          <ul className="mt-8 flex animate-rise flex-wrap gap-3 [animation-delay:240ms]">
            {stats.map((stat) => (
              <li key={stat.label} className="rounded-2xl bg-white/[0.04] px-4 py-2.5 ring-1 ring-white/10">
                <span className="font-display text-xl font-bold">{stat.value}</span>{" "}
                <span className="text-sm text-mist-300">{stat.label}</span>
              </li>
            ))}
          </ul>

          <div className="mt-10 flex animate-rise flex-wrap gap-3 [animation-delay:320ms]">
            <a
              href="#team"
              className="group inline-flex items-center gap-2 rounded-full bg-indigo-500 px-5 py-3 font-semibold text-white shadow-[0_10px_40px_-10px_#6366f1] transition hover:bg-indigo-400"
            >
              Meet the team
              <span aria-hidden className="transition-transform group-hover:translate-y-0.5">
                ↓
              </span>
            </a>
            <a
              href={REPO_URL}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-2 rounded-full bg-white/5 px-5 py-3 font-semibold ring-1 ring-white/15 transition hover:bg-white/10"
            >
              <GitHubIcon />
              View the code
            </a>
          </div>

          <p className="mt-6 animate-rise text-sm text-mist-400 [animation-delay:400ms]">
            Tip: click <span className="text-mist-200">{human.name}</span> in the middle of the orbit to say hi 👋
          </p>
        </div>

        <div className="animate-rise [animation-delay:200ms]">
          <TeamOrbit team={team} human={human} />
        </div>
      </div>
    </section>
  );
}

function Aurora() {
  return (
    <div aria-hidden className="pointer-events-none absolute inset-0 -z-10">
      <div className="absolute -top-48 left-[-12%] size-[560px] animate-drift rounded-full bg-indigo-600/25 blur-[120px]" />
      <div className="absolute right-[-18%] top-24 size-[520px] animate-drift rounded-full bg-fuchsia-500/15 blur-[120px] [animation-delay:-6s]" />
      <div className="absolute bottom-[-35%] left-1/3 size-[460px] animate-drift rounded-full bg-cyan-500/10 blur-[120px] [animation-delay:-12s]" />
      <div className="absolute inset-0 bg-[radial-gradient(rgba(255,255,255,0.07)_1px,transparent_1px)] [background-size:28px_28px] [mask-image:radial-gradient(ellipse_at_center,black_25%,transparent_75%)]" />
    </div>
  );
}

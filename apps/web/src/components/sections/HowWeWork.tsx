import { SectionHeading } from "@/components/sections/SectionHeading";

const STEPS = [
  {
    emoji: "✍️",
    title: "The human writes the core",
    text: "Retrieval and fusion, the agent loop, router fallback, eval metrics and guardrail checks are hand-written — so every line can be explained and defended.",
  },
  {
    emoji: "🤖",
    title: "Teammates scaffold, test and review",
    text: "Each teammate is a Claude Code subagent with its own brief and least-privilege tools. They build the plumbing, write the tests and review critically.",
  },
  {
    emoji: "📏",
    title: "Evals decide what merges",
    text: "Every change to prompts, retrieval, routing or tools ships with measured eval deltas. Real numbers only — or no claim at all.",
  },
];

export function HowWeWork() {
  return (
    <section id="how" aria-labelledby="how-title" className="mx-auto max-w-6xl px-4 py-24 sm:px-6">
      <SectionHeading id="how-title" eyebrow="How we work" title="AI does the plumbing. The human owns the brain.">
        A learning-first workflow: the parts an AI engineer must deeply understand stay hand-written,
        and everything is measured before it is claimed.
      </SectionHeading>

      <ol className="mt-12 grid gap-5 md:grid-cols-3">
        {STEPS.map((step, index) => (
          <li key={step.title} className="reveal">
            <div className="group relative h-full rounded-3xl bg-ink-900/70 p-6 ring-1 ring-white/10 transition hover:ring-white/20">
                <div className="flex items-center justify-between">
                  <span className="grid size-12 place-items-center rounded-2xl bg-white/5 text-2xl ring-1 ring-white/10 transition-transform duration-300 group-hover:-rotate-6 group-hover:scale-110">
                    {step.emoji}
                  </span>
                  <span className="font-mono text-sm text-mist-400">0{index + 1}</span>
                </div>
                <h3 className="mt-5 font-display text-lg font-semibold">{step.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-mist-300">{step.text}</p>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

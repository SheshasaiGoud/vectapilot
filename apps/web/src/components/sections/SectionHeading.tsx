import type { ReactNode } from "react";

export function SectionHeading({
  id,
  eyebrow,
  title,
  children,
}: {
  id: string;
  eyebrow: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="reveal max-w-2xl">
      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-300">{eyebrow}</p>
      <h2 id={id} className="mt-3 font-display text-3xl font-bold tracking-tight sm:text-4xl">
        {title}
      </h2>
      <p className="mt-4 leading-relaxed text-mist-300">{children}</p>
    </div>
  );
}

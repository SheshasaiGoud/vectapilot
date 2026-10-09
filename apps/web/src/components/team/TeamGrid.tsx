"use client";

import { useReducedMotion, useSpring } from "motion/react";
import * as m from "motion/react-m";
import type { CSSProperties, PointerEvent } from "react";

import { Avatar } from "@/components/team/Avatar";
import { QuipBubble } from "@/components/team/QuipBubble";
import { StatusBadge } from "@/components/team/StatusBadge";
import { useTeamDialog } from "@/components/team/TeamDialogProvider";
import type { Teammate } from "@/lib/types";

export function TeamGrid({ team }: { team: Teammate[] }) {
  const { open } = useTeamDialog();
  return (
    <ul className="grid gap-5 sm:grid-cols-2 xl:grid-cols-4">
      {team.map((member, index) => (
        // `reveal` (CSS, scroll-driven) sits on the <li>; Motion owns the card's own transform.
        <li key={member.id} className="reveal" style={{ "--accent": member.accent } as CSSProperties}>
          <TeammateCard member={member} index={index} onOpen={open} />
        </li>
      ))}
    </ul>
  );
}

const TILT_DEGREES = 8;
const spring = { stiffness: 220, damping: 18 };

function TeammateCard({
  member,
  index,
  onOpen,
}: {
  member: Teammate;
  index: number;
  onOpen: (id: string) => void;
}) {
  const reduceMotion = useReducedMotion();
  const rotateX = useSpring(0, spring);
  const rotateY = useSpring(0, spring);

  function handlePointerMove(event: PointerEvent<HTMLElement>) {
    if (reduceMotion || event.pointerType !== "mouse") return;
    const rect = event.currentTarget.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width;
    const y = (event.clientY - rect.top) / rect.height;
    rotateY.set((x - 0.5) * TILT_DEGREES * 2);
    rotateX.set((0.5 - y) * TILT_DEGREES * 2);
    // The spotlight follows the cursor via CSS variables — no React re-render needed.
    event.currentTarget.style.setProperty("--x", `${x * 100}%`);
    event.currentTarget.style.setProperty("--y", `${y * 100}%`);
  }

  function handlePointerLeave() {
    rotateX.set(0);
    rotateY.set(0);
  }

  return (
    <m.article
      whileHover={{ y: -6 }}
      style={{ rotateX, rotateY, transformPerspective: 900 }}
      onPointerMove={handlePointerMove}
      onPointerLeave={handlePointerLeave}
      className="group relative flex h-full flex-col rounded-3xl bg-ink-900/80 p-5 ring-1 ring-white/10 transition-shadow duration-300 hover:shadow-[0_24px_60px_-24px_var(--accent)]"
    >
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 rounded-3xl opacity-0 transition-opacity duration-300 group-hover:opacity-100"
        style={{
          background:
            "radial-gradient(380px circle at var(--x, 50%) var(--y, 50%), color-mix(in oklab, var(--accent) 16%, transparent), transparent 65%)",
        }}
      />

      <div className="relative flex items-start justify-between gap-3">
        <div className="relative w-16 shrink-0">
          {member.status.kind === "active" && (
            <span aria-hidden className="absolute inset-0 animate-pulse-ring rounded-2xl border-2 border-[var(--accent)]" />
          )}
          <div className={`overflow-hidden rounded-2xl ring-2 ring-[var(--accent)] ${member.status.kind === "standby" ? "opacity-75" : ""}`}>
            <Avatar src={member.avatarSrc} size={64} />
          </div>
          <span
            aria-hidden
            className="absolute -bottom-1 -right-2 origin-[70%_70%] text-xl opacity-0 transition-opacity group-hover:animate-wave group-hover:opacity-100"
          >
            👋
          </span>
        </div>
        <StatusBadge status={member.status} />
      </div>

      {/* Stretched button: the whole card is clickable, but the accessible control is the name.
          The heading must not be `relative`, so the button's ::after fills the article. */}
      <h3 className="mt-4 font-display text-lg font-semibold">
        <button
          type="button"
          onClick={() => onOpen(member.id)}
          aria-haspopup="dialog"
          className="text-left after:absolute after:inset-0 after:z-10 after:rounded-3xl focus-visible:outline-none focus-visible:after:ring-2 focus-visible:after:ring-indigo-300"
        >
          {member.name}
        </button>
      </h3>
      <p className="relative text-sm font-medium text-[var(--accent)]">{member.role}</p>
      <p className="relative mt-3 text-sm leading-relaxed text-mist-300">{member.bio}</p>

      <QuipBubble quips={member.quips} status={member.status} offset={index} />

      <div className="relative mt-auto flex items-center justify-between gap-2 pt-5">
        <ul className="flex flex-wrap gap-1.5" aria-label="Owned milestones">
          {member.milestones.map((m) => (
            <li key={m.id} title={m.title} className="rounded-md bg-white/5 px-1.5 py-0.5 font-mono text-[11px] text-mist-200 ring-1 ring-white/10">
              {m.id}
            </li>
          ))}
        </ul>
        <span aria-hidden className="text-sm text-mist-400 transition-all group-hover:translate-x-1 group-hover:text-[var(--accent)]">
          →
        </span>
      </div>
    </m.article>
  );
}

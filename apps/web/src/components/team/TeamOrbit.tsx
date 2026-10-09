"use client";

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState, type CSSProperties } from "react";

import { Avatar } from "@/components/team/Avatar";
import { useTeamDialog } from "@/components/team/TeamDialogProvider";
import type { HumanLead, Teammate } from "@/lib/types";

/** Ring geometry in % of the orbit's width. Teammates on the current milestone orbit closest. */
const INNER = { radius: 25, size: 15, seconds: 48 };
const OUTER = { radius: 42, size: 13, seconds: 84 };

type RingConfig = typeof INNER;

export function TeamOrbit({ team, human }: { team: Teammate[]; human: HumanLead }) {
  const { open } = useTeamDialog();
  const [wave, setWave] = useState(0);
  const [greeting, setGreeting] = useState(false);

  useEffect(() => {
    if (!greeting) return;
    const timer = setTimeout(() => setGreeting(false), 2200);
    return () => clearTimeout(timer);
  }, [greeting, wave]);

  const inner = team.filter((m) => m.status.kind === "active");
  const outer = team.filter((m) => m.status.kind !== "active");

  return (
    <div className="orbit relative mx-auto aspect-square w-full max-w-[540px]">
      <div aria-hidden className="absolute inset-[20%] rounded-full bg-indigo-500/25 blur-3xl" />
      <div aria-hidden className="absolute rounded-full border border-white/10" style={{ inset: `${50 - INNER.radius}%` }} />
      <div
        aria-hidden
        className="absolute rounded-full border border-dashed border-white/10"
        style={{ inset: `${50 - OUTER.radius}%` }}
      />

      <Ring members={inner} config={INNER} clockwise onOpen={open} wave={wave} waveOffset={0} />
      <Ring members={outer} config={OUTER} clockwise={false} onOpen={open} wave={wave} waveOffset={inner.length} />

      {/* The human lead sits at the centre. Clicking makes the crew wave. */}
      <div className="absolute left-1/2 top-1/2 w-[24%] -translate-x-1/2 -translate-y-1/2">
        <span aria-hidden className="absolute inset-0 animate-pulse-ring rounded-full border-2 border-indigo-400/60" />
        <span
          aria-hidden
          className="absolute inset-0 animate-pulse-ring rounded-full border-2 border-indigo-400/60 [animation-delay:1.2s]"
        />
        <motion.button
          type="button"
          onClick={() => {
            setWave((w) => w + 1);
            setGreeting(true);
          }}
          whileHover={{ scale: 1.06 }}
          whileTap={{ scale: 0.94 }}
          aria-label={`${human.name}, ${human.role}. Say hi to the team.`}
          className="relative block w-full overflow-hidden rounded-full shadow-[0_0_70px_-10px_#818cf8] ring-4 ring-indigo-400/70"
        >
          <Avatar src={human.avatarSrc} size={130} eager />
        </motion.button>
        <span className="pointer-events-none absolute -bottom-2.5 left-1/2 -translate-x-1/2 whitespace-nowrap rounded-full bg-indigo-500 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-white shadow-lg">
          {human.name} · lead
        </span>
        <AnimatePresence>
          {greeting && (
            <motion.p
              key={wave}
              role="status"
              className="pointer-events-none absolute bottom-full left-1/2 mb-3 -translate-x-1/2 whitespace-nowrap rounded-full bg-white px-3 py-1 text-xs font-semibold text-ink-950 shadow-xl"
              initial={{ opacity: 0, y: 8, scale: 0.8 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -6 }}
            >
              👋 The crew says hi, {human.name}!
            </motion.p>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}

function Ring({
  members,
  config,
  clockwise,
  onOpen,
  wave,
  waveOffset,
}: {
  members: Teammate[];
  config: RingConfig;
  clockwise: boolean;
  onOpen: (id: string) => void;
  wave: number;
  waveOffset: number;
}) {
  // The ring spins one way; each avatar spins the opposite way at the same speed, so it stays upright.
  const spin = clockwise ? "animate-orbit" : "animate-orbit-reverse";
  const counterSpin = clockwise ? "animate-orbit-reverse" : "animate-orbit";
  const duration = { animationDuration: `${config.seconds}s` } satisfies CSSProperties;

  return (
    <ul className={`orbit-spin absolute inset-0 ${spin}`} style={duration}>
      {members.map((member, i) => {
        const angle = (i / members.length) * 2 * Math.PI - Math.PI / 2 + (clockwise ? 0 : Math.PI / members.length);
        // Rounded so server-rendered and browser-computed styles match exactly.
        const left = (50 + config.radius * Math.cos(angle)).toFixed(3);
        const top = (50 + config.radius * Math.sin(angle)).toFixed(3);
        return (
          <li
            key={member.id}
            className="absolute -translate-x-1/2 -translate-y-1/2"
            style={{ left: `${left}%`, top: `${top}%`, width: `${config.size}%` }}
          >
            <div className={`orbit-spin ${counterSpin}`} style={duration}>
              <OrbitAvatar member={member} onOpen={onOpen} wave={wave} delay={(waveOffset + i) * 0.07} />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function OrbitAvatar({
  member,
  onOpen,
  wave,
  delay,
}: {
  member: Teammate;
  onOpen: (id: string) => void;
  wave: number;
  delay: number;
}) {
  const { kind } = member.status;
  return (
    <motion.button
      type="button"
      onClick={() => onOpen(member.id)}
      aria-haspopup="dialog"
      aria-label={`${member.name}, ${member.role}. ${member.status.label}.`}
      whileHover={{ scale: 1.14 }}
      whileTap={{ scale: 0.94 }}
      className="group/avatar relative block w-full rounded-full"
      style={{ "--accent": member.accent } as CSSProperties}
    >
      {/* Re-keyed on every wave so the hop replays; nothing plays on first render. */}
      <motion.span
        key={wave}
        className="relative block"
        initial={false}
        animate={wave > 0 ? { y: [0, -16, 0], rotate: [0, -10, 8, 0] } : undefined}
        transition={{ duration: 0.7, delay, ease: "easeOut" }}
      >
        {kind === "active" && (
          <span aria-hidden className="absolute inset-0 animate-pulse-ring rounded-full border-2 border-[var(--accent)]" />
        )}
        <span
          className={`block overflow-hidden rounded-full ring-2 ring-[var(--accent)] shadow-[0_8px_30px_-8px_var(--accent)] ${kind === "standby" ? "opacity-70" : ""}`}
        >
          <Avatar src={member.avatarSrc} size={80} eager />
        </span>
        {kind === "standby" && (
          <span aria-hidden className="absolute -right-1 -top-1 animate-float text-sm">
            💤
          </span>
        )}
      </motion.span>
      <span className="pointer-events-none absolute left-1/2 top-full z-20 mt-2 -translate-x-1/2 whitespace-nowrap rounded-full bg-ink-800/95 px-2.5 py-1 text-xs opacity-0 ring-1 ring-white/10 transition-opacity group-hover/avatar:opacity-100 group-focus-visible/avatar:opacity-100">
        <span className="font-semibold">{member.firstName}</span>
        <span className="text-mist-400"> · {member.role}</span>
      </span>
    </motion.button>
  );
}

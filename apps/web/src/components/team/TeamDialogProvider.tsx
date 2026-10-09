"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";

import { Avatar } from "@/components/team/Avatar";
import { StatusBadge } from "@/components/team/StatusBadge";
import type { Teammate } from "@/lib/types";

interface TeamDialogApi {
  open: (id: string) => void;
}

const TeamDialogContext = createContext<TeamDialogApi | null>(null);

/** Lets any card, orbit avatar or roadmap chip open a teammate's brief. */
export function useTeamDialog() {
  const api = useContext(TeamDialogContext);
  if (!api) throw new Error("useTeamDialog must be used inside <TeamDialogProvider>");
  return api;
}

export function TeamDialogProvider({ team, children }: { team: Teammate[]; children: ReactNode }) {
  const [isOpen, setIsOpen] = useState(false);
  // Kept after closing so the exit animation still has content to show.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const member = team.find((m) => m.id === selectedId) ?? null;

  const open = useCallback((id: string) => {
    setSelectedId(id);
    setIsOpen(true);
  }, []);
  const api = useMemo(() => ({ open }), [open]);

  return (
    <TeamDialogContext value={api}>
      {children}
      <Dialog.Root open={isOpen} onOpenChange={setIsOpen}>
        <AnimatePresence>
          {isOpen && member && (
            <Dialog.Portal forceMount>
              <Dialog.Overlay asChild forceMount>
                <m.div
                  className="fixed inset-0 z-50 bg-ink-950/75 backdrop-blur-sm"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                />
              </Dialog.Overlay>
              <div className="pointer-events-none fixed inset-0 z-50 flex items-end justify-center sm:items-center sm:p-6">
                <Dialog.Content asChild forceMount>
                  <m.div
                    className="pointer-events-auto relative max-h-[88vh] w-full overflow-y-auto rounded-t-3xl bg-ink-900 ring-1 ring-white/10 sm:max-w-xl sm:rounded-3xl"
                    initial={{ opacity: 0, y: 40, scale: 0.96 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    exit={{ opacity: 0, y: 24, scale: 0.98 }}
                    transition={{ type: "spring", stiffness: 320, damping: 30 }}
                  >
                    <TeammateBrief member={member} />
                  </m.div>
                </Dialog.Content>
              </div>
            </Dialog.Portal>
          )}
        </AnimatePresence>
      </Dialog.Root>
    </TeamDialogContext>
  );
}

const section = {
  hidden: { opacity: 0, y: 10 },
  show: { opacity: 1, y: 0 },
};

function TeammateBrief({ member }: { member: Teammate }) {
  const command = `@agent-${member.id} ${member.samplePrompt}`;

  return (
    <>
      <div
        className="relative flex items-center gap-4 px-6 pb-5 pt-7"
        style={
          {
            "--accent": member.accent,
            background: `linear-gradient(135deg, ${member.accent}33, transparent 65%)`,
          } as CSSProperties
        }
      >
        <m.div
          className="w-20 shrink-0 overflow-hidden rounded-2xl ring-2 ring-[var(--accent)]"
          initial={{ scale: 0.6, rotate: -10 }}
          animate={{ scale: 1, rotate: 0 }}
          transition={{ type: "spring", stiffness: 260, damping: 14, delay: 0.05 }}
        >
          <Avatar src={member.avatarSrc} size={80} />
        </m.div>
        <div className="min-w-0">
          <Dialog.Title className="font-display text-2xl font-bold">{member.name}</Dialog.Title>
          <p className="text-sm font-medium" style={{ color: member.accent }}>
            {member.role}
          </p>
          <div className="mt-2">
            <StatusBadge status={member.status} />
          </div>
        </div>
        <Dialog.Close
          className="absolute right-4 top-4 grid size-9 place-items-center rounded-full bg-white/5 text-mist-200 ring-1 ring-white/10 transition hover:bg-white/10"
          aria-label="Close"
        >
          <span aria-hidden className="text-xl leading-none">
            ×
          </span>
        </Dialog.Close>
      </div>

      <m.div
        className="space-y-6 px-6 pb-7"
        initial="hidden"
        animate="show"
        transition={{ staggerChildren: 0.06, delayChildren: 0.1 }}
      >
        <m.div variants={section}>
          <Dialog.Description className="text-mist-200">{member.bio}</Dialog.Description>
        </m.div>

        <m.section variants={section}>
          <BriefHeading>Owns</BriefHeading>
          <ul className="flex flex-wrap gap-2">
            {member.milestones.map((m) => (
              <li key={m.id} className="rounded-lg bg-white/5 px-2.5 py-1 text-xs ring-1 ring-white/10">
                <span className="font-mono font-semibold" style={{ color: member.accent }}>
                  {m.id}
                </span>{" "}
                {m.title}
              </li>
            ))}
          </ul>
        </m.section>

        <m.section variants={section}>
          <BriefHeading>Principles</BriefHeading>
          <ul className="space-y-1.5 text-sm text-mist-200">
            {member.principles.map((p) => (
              <li key={p} className="flex gap-2">
                <span aria-hidden style={{ color: member.accent }}>
                  ✦
                </span>
                {p}
              </li>
            ))}
          </ul>
        </m.section>

        <m.section variants={section}>
          <BriefHeading>Permissions</BriefHeading>
          <ul className="flex flex-wrap gap-1.5">
            {member.tools.map((tool) => (
              <li key={tool} className="rounded-md bg-ink-800 px-2 py-0.5 font-mono text-xs text-mist-200 ring-1 ring-white/10">
                {tool}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-mist-400">
            Read live from <code className="font-mono">{member.agentPath}</code> · model:{" "}
            <code className="font-mono">{member.model}</code>. Least privilege — only the tools this role needs.
          </p>
        </m.section>

        <m.section variants={section}>
          <BriefHeading>Try it in Claude Code</BriefHeading>
          <CopyCommand command={command} />
        </m.section>

        <m.a
          variants={section}
          href={member.agentUrl}
          target="_blank"
          rel="noreferrer"
          className="group inline-flex items-center gap-1.5 text-sm font-medium text-indigo-300 hover:text-indigo-200"
        >
          Read {member.firstName}&apos;s full brief on GitHub
          <span aria-hidden className="transition-transform group-hover:translate-x-1">
            →
          </span>
        </m.a>
      </m.div>
    </>
  );
}

function BriefHeading({ children }: { children: ReactNode }) {
  return <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-mist-400">{children}</h3>;
}

function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard can be blocked (e.g. insecure context); the command stays visible to copy by hand.
    }
  }

  return (
    <div className="flex items-start gap-2 rounded-xl bg-ink-950 p-3 ring-1 ring-white/10">
      <code className="min-w-0 flex-1 break-words font-mono text-[13px] leading-relaxed text-emerald-200">
        <span className="select-none text-mist-400">&gt; </span>
        {command}
      </code>
      <button
        type="button"
        onClick={copy}
        className="shrink-0 rounded-lg bg-white/5 px-2.5 py-1 text-xs text-mist-200 ring-1 ring-white/10 transition hover:bg-white/10"
      >
        <span aria-live="polite">{copied ? "Copied ✓" : "Copy"}</span>
      </button>
    </div>
  );
}

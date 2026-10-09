import fs from "node:fs";
import path from "node:path";

import palette from "@/data/palette.json";
import { humanLead, personas } from "@/data/personas";
import { currentMilestone, milestones } from "@/data/roadmap";
import { readAgentFiles } from "@/lib/agents";
import type { HumanLead, MilestoneView, Status, Teammate } from "@/lib/types";

export const REPO_URL = "https://github.com/SheshasaiGoud/vectapilot";

/** Set to `/vectapilot` when building for GitHub Pages; empty locally. */
const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? "";
const AVATAR_DIR = path.join(process.cwd(), "public/avatars");

type PaletteColor = keyof typeof palette;

function avatarSrc(id: string) {
  return `${BASE_PATH}/avatars/${id}.svg`;
}

function isPaletteColor(color: string): color is PaletteColor {
  return color in palette;
}

/** Status is derived from the roadmap, never hand-written, so it cannot go stale. */
function statusFor(id: string): Status {
  const now = milestones.findIndex((m) => m.id === currentMilestone);
  if (milestones[now].owners.includes(id)) {
    return { kind: "active", label: `On ${currentMilestone} now` };
  }
  const upcoming = milestones.slice(now + 1).find((m) => m.owners.includes(id));
  if (!upcoming) return { kind: "standby", label: "Off duty" };
  const distance = milestones.indexOf(upcoming) - now;
  return distance <= 2
    ? { kind: "next", label: `Up next · ${upcoming.id}` }
    : { kind: "standby", label: `Standby until ${upcoming.id}` };
}

/**
 * Joins the agent files, personas, palette and roadmap into what the page renders.
 * Any drift between them fails the build with a list of every problem found.
 */
export function getTeamData(): {
  team: Teammate[];
  human: HumanLead;
  roadmap: MilestoneView[];
} {
  const agents = new Map(readAgentFiles().map((agent) => [agent.id, agent]));
  const problems: string[] = [];

  for (const id of agents.keys()) {
    if (!personas[id]) problems.push(`agent "${id}" has no persona in src/data/personas.ts`);
  }
  for (const id of Object.keys(personas)) {
    if (!agents.has(id)) problems.push(`persona "${id}" has no file in .claude/agents/`);
  }
  for (const milestone of milestones) {
    for (const owner of milestone.owners) {
      if (!personas[owner]) problems.push(`${milestone.id} owner "${owner}" is not a teammate`);
    }
  }
  if (!milestones.some((m) => m.id === currentMilestone)) {
    problems.push(`currentMilestone "${currentMilestone}" is not in the roadmap`);
  }
  for (const id of [...agents.keys(), "human-lead"]) {
    if (!fs.existsSync(path.join(AVATAR_DIR, `${id}.svg`))) {
      problems.push(`missing public/avatars/${id}.svg — run "npm run avatars"`);
    }
  }
  for (const agent of agents.values()) {
    if (!isPaletteColor(agent.color)) {
      problems.push(`agent "${agent.id}" has unknown color "${agent.color}"`);
    }
  }
  if (problems.length > 0) {
    throw new Error(`Team drift detected:\n  - ${problems.join("\n  - ")}`);
  }

  const team: Teammate[] = Object.entries(personas).map(([id, persona]) => {
    const agent = agents.get(id)!;
    const colors = palette[agent.color as PaletteColor];
    return {
      id,
      name: persona.name,
      firstName: persona.name.split(" ")[0],
      role: persona.role,
      bio: persona.bio,
      principles: [...persona.principles],
      quips: persona.quips,
      samplePrompt: persona.samplePrompt,
      accent: colors.accent,
      tint: colors.tint,
      avatarSrc: avatarSrc(id),
      tools: agent.tools,
      model: agent.model,
      agentPath: agent.repoPath,
      agentUrl: `${REPO_URL}/blob/main/${agent.repoPath}`,
      milestones: milestones
        .filter((m) => m.owners.includes(id))
        .map(({ id: milestoneId, title }) => ({ id: milestoneId, title })),
      status: statusFor(id),
    };
  });

  const byId = new Map(team.map((member) => [member.id, member]));
  const now = milestones.findIndex((m) => m.id === currentMilestone);

  const roadmap: MilestoneView[] = milestones.map((milestone, index) => ({
    id: milestone.id,
    title: milestone.title,
    summary: milestone.summary,
    state: index < now ? "done" : index === now ? "current" : "upcoming",
    owners: milestone.owners.map((owner) => {
      const member = byId.get(owner)!;
      return {
        id: owner,
        firstName: member.firstName,
        accent: member.accent,
        avatarSrc: member.avatarSrc,
      };
    }),
  }));

  const human: HumanLead = {
    name: humanLead.name,
    handle: humanLead.handle,
    role: humanLead.role,
    accent: palette[humanLead.color].accent,
    tint: palette[humanLead.color].tint,
    avatarSrc: avatarSrc("human-lead"),
  };

  return { team, human, roadmap };
}

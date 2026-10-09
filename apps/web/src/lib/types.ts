/** Shapes shared between the build-time data layer and the client components. */

export type StatusKind = "active" | "next" | "standby";

export interface Status {
  kind: StatusKind;
  label: string;
}

export interface MilestoneRef {
  id: string;
  title: string;
}

export interface Teammate {
  /** Subagent name from `.claude/agents/<file>.md` — also the avatar seed. */
  id: string;
  name: string;
  firstName: string;
  role: string;
  bio: string;
  principles: string[];
  quips: string[];
  /** Example request to try in Claude Code. */
  samplePrompt: string;
  accent: string;
  tint: string;
  avatarSrc: string;
  /** Read from the agent file's frontmatter. */
  tools: string[];
  model: string;
  agentPath: string;
  agentUrl: string;
  milestones: MilestoneRef[];
  status: Status;
}

export interface HumanLead {
  name: string;
  handle: string;
  role: string;
  accent: string;
  tint: string;
  avatarSrc: string;
}

export type MilestoneState = "done" | "current" | "upcoming";

export interface MilestoneOwner {
  id: string;
  firstName: string;
  accent: string;
  avatarSrc: string;
}

export interface MilestoneView {
  id: string;
  title: string;
  summary: string;
  state: MilestoneState;
  owners: MilestoneOwner[];
}

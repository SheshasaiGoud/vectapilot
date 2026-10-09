import fs from "node:fs";
import path from "node:path";

/**
 * Reads the real Claude Code subagent files at build time.
 *
 * Synchronous on purpose: with `output: "export"` this runs once during `next build`, and the
 * result is baked into the static HTML. (`"use cache"` is a runtime cache and is not supported
 * in static exports — see docs/adr/0002.)
 */

export interface AgentFile {
  id: string;
  description: string;
  tools: string[];
  model: string;
  color: string;
  /** Path relative to the repository root, e.g. `.claude/agents/maya-tech-lead.md`. */
  repoPath: string;
}

/** `npm run build` / `npm run dev` run from apps/web, so the repo root is two levels up. */
export const REPO_ROOT = path.resolve(process.cwd(), "../..");
const AGENTS_DIR = path.join(REPO_ROOT, ".claude/agents");

/** Minimal parser for the flat `key: value` frontmatter our agent files use. */
function parseFrontmatter(source: string, fileName: string): Record<string, string> {
  const match = /^---\r?\n([\s\S]*?)\r?\n---/.exec(source);
  if (!match) throw new Error(`${fileName}: missing YAML frontmatter`);

  const fields: Record<string, string> = {};
  for (const line of match[1].split(/\r?\n/)) {
    const colon = line.indexOf(":");
    if (colon === -1) continue;
    fields[line.slice(0, colon).trim()] = line.slice(colon + 1).trim();
  }
  return fields;
}

export function readAgentFiles(): AgentFile[] {
  return fs
    .readdirSync(AGENTS_DIR)
    .filter((file) => file.endsWith(".md"))
    .sort()
    .map((file) => {
      const fields = parseFrontmatter(fs.readFileSync(path.join(AGENTS_DIR, file), "utf8"), file);
      if (!fields.name || !fields.description) {
        throw new Error(`${file}: "name" and "description" are required`);
      }
      return {
        id: fields.name,
        description: fields.description,
        // Claude Code inherits every tool when `tools` is omitted.
        tools: fields.tools ? fields.tools.split(",").map((tool) => tool.trim()) : ["all tools"],
        model: fields.model ?? "default",
        color: fields.color ?? "",
        repoPath: `.claude/agents/${file}`,
      };
    });
}

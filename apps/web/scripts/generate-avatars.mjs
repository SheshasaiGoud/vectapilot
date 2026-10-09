// Generates one SVG avatar per teammate (plus the human lead) into public/avatars/.
// Run with `npm run avatars` after adding, renaming or recolouring a teammate.
// Output is deterministic (seeded by the agent name), so the files are committed.
// Artwork: "Notionists" by Zoish, CC0 1.0, via DiceBear.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { createAvatar } from "@dicebear/core";
import * as notionists from "@dicebear/notionists";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const agentsDir = path.resolve(webRoot, "../../.claude/agents");
const outDir = path.join(webRoot, "public/avatars");
const palette = JSON.parse(fs.readFileSync(path.join(webRoot, "src/data/palette.json"), "utf8"));

/** Reads one `key: value` line from an agent file's frontmatter. */
function field(source, key) {
  return new RegExp(`^${key}:\\s*(.+)$`, "m").exec(source)?.[1].trim();
}

const people = fs
  .readdirSync(agentsDir)
  .filter((file) => file.endsWith(".md"))
  .map((file) => {
    const source = fs.readFileSync(path.join(agentsDir, file), "utf8");
    return { id: field(source, "name"), color: field(source, "color") };
  });
people.push({ id: "human-lead", color: "indigo" });

fs.mkdirSync(outDir, { recursive: true });

for (const { id, color } of people) {
  const colors = palette[color];
  if (!id || !colors) throw new Error(`Cannot build avatar for "${id}" (color "${color}")`);

  const svg = createAvatar(notionists, {
    seed: id,
    backgroundColor: [colors.tint.slice(1)],
    gestureProbability: 0,
  }).toString();

  fs.writeFileSync(path.join(outDir, `${id}.svg`), svg);
  console.log(`  ✓ ${id}.svg`);
}
console.log(`Generated ${people.length} avatars in public/avatars/`);

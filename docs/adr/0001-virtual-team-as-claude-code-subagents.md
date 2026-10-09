# ADR 0001 — Virtual team as Claude Code subagents

- **Status:** accepted · 2026-10-09

**Context.** VectaPilot is built by one human with AI help. We want specialised, consistent AI
"teammates" that respect learning mode (the human hand-writes the core AI logic) and that can be
shown on the project website.

**Options.** (a) One general AI assistant with ad-hoc prompts; (b) a multi-agent framework
(LangGraph, CrewAI) running our own agents; (c) Claude Code subagents — Markdown files in
`.claude/agents/` with a role prompt, a tool allow-list and a colour.

**Decision.** (c). Eight subagents, one per area of the playbook. Rules shared by all of them live
once in `CLAUDE.md` (which every subagent loads); each agent file holds only its role, ownership and
learning-mode boundary. Tools are least privilege per role.

**Consequences.** No extra framework or runtime; the team is versioned in git and reviewable like
code. The team is a *development* aid, not part of the product. Because the website reads these
files at build time, the published team cannot drift from the real one.

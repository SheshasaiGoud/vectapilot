"""The agent: one customer message → a reply, a proposed write, an answer or a hand-off (ADR 0016
decisions 4, 5 and 7; M5 PR 4).

- `types` — the loop's interface: the model's step variants, the transcript, the turn's outcomes,
  `LoopLimits` and the two protocols the loop calls (`StepModel`, `ToolRunner`).
- `steps` — `build_agent_step`: one business's `AgentStep` schema, built from its enabled tools.
- `prompt` — the versioned step prompt (`AGENT_PROMPT_VERSION`), its boundary blocks recomputed at
  every step.
- `step_model` — `GatewayStepModel`: one structured call per step through the agent's own gateway.
- `tools` — `BoundTools`: one business's executor bound to one request's context (reads and
  `propose` only, W1).
- `loop` — `run_turn`, the loop itself: **[HAND-WRITE], shesha's M5 H1** (guide:
  docs/learning/m5-agent-loop.md). A stub until then.
- `routing` — `agent_router`: M4's `route_message` in front of the loop (D7), and the startup check
  of its classifier artifact.
- `profiles` — each business's agent profile: tools, schema, route policy and whether W11 applies.
- `service` and `router` — `POST /v1/agent`: the turn timeout, every exception a hand-off, W11.

Nothing here executes a write: the loop can read and propose, and only the confirm route
(`actions/confirm.py`) executes a stored, confirmed action (W1, W2).
"""

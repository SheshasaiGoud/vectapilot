"""H1 (M5) — the agent loop. [HAND-WRITE], written by shesha (D5 (b)); time-box 2 h.

Guide: docs/learning/m5-agent-loop.md · Tests: tests/agent/test_loop.py (strict xfails until the
body is written, switched by `PENDING_HAND_WRITE` in tests/canary/read_paths.py). ADR 0016
decision 5; the plan's "H1" section (docs/plans/m5-pr-checks.md).

Built by the team and tested: everything `run_turn` reads and returns (`agent/types.py`: the step
variants, `Transcript`, `Observation.of`, the outcomes, `LoopLimits`, `FAILURE_CODES`, the
`StepModel` and `ToolRunner` protocols), the real step model (`agent/step_model.py`), the real
tool runner (`agent/tools.py`) and the service around the loop (`agent/service.py`: the turn
timeout, every exception a hand-off, W11). The body of `run_turn` is shesha's.
"""

from vectapilot_api.agent.types import LoopLimits, StepModel, ToolRunner, TurnOutcome


async def run_turn(
    message: str, *, step_model: StepModel, tools: ToolRunner, limits: LoopLimits
) -> TurnOutcome:
    """Run one customer message to its outcome: a reply, a write proposal, or a hand-off.

    Contract (ADR 0016 decision 5; the plan's "H1"). The loop never sees `business_id` or
    `customer_ref`: `step_model` and `tools` are bound to them.

    1. **Step cap.** Ask `step_model.next_step(transcript)` at most `limits.max_steps` times,
       starting from `Transcript(message)`. Every step's call is carried out, the last one's too
       (so a valid proposal on the last allowed step still ends the turn with
       `ConfirmationRequired`). If the cap is reached without an ending step, return
       `TurnHandoff(LoopHandoffReason.STEP_CAP)`.
    2. **`Reply` and `Handoff` end the turn as they are:** `TurnReply(step.text)`, or
       `TurnHandoff(step.reason)` with the model's own reason (`safety` included: the service, not
       the loop, makes it urgent and adds the business's guidance).
    3. **`CallTool` for a read tool** (`tools.kind(call.tool_name)` is `ToolKind.READ`): `envelope =
       await tools.run(call.tool_name, call.argument_values())`, then append
       `Observation.of(call, envelope)` to the transcript and ask for the next step. Only
       `envelope.observation` ever reaches the model (W10).
    4. **`CallTool` for a write tool** (`ToolKind.WRITE`): `await tools.propose(...)`, never
       `run`. A valid proposal (`envelope.proposal` is not None) ends the turn with
       `ConfirmationRequired(envelope.proposal)`. A refusal (`not_found`, `slot_unavailable`, ...)
       is an observation like a read's, and the loop goes on.
    5. **Failures.** A typed error whose code is in `FAILURE_CODES` (`invalid_arguments`,
       `unavailable`, `timeout`) is a *failure* of that tool. The first failure of a tool in this
       turn is an observation; the second failure of the **same** tool (reads and proposals
       counted together, by tool name) returns `TurnHandoff(LoopHandoffReason.TOOL_FAILURE)`
       (business-context §3.5). Every other code (`not_found`, `slot_unavailable`,
       `not_enabled`) is a normal observation and never counts.
    6. **The model's errors.** `StructuredOutputInvalid` from `next_step` (schema-invalid output
       after the gateway's one repair) returns `TurnHandoff(INVALID_MODEL_OUTPUT)`; any other
       `GatewayError` returns `TurnHandoff(MODEL_UNAVAILABLE)`. Catch `StructuredOutputInvalid`
       first: it is a `GatewayError` too. Anything else from `next_step` propagates.
    7. **Any other exception from `tools`** (`run`, `propose` or `kind`; a bug, a database error
       the executor did not type) returns `TurnHandoff(LoopHandoffReason.TOOL_FAILURE)`, never a
       reply.
    8. **What propagates, and what is never here.** `asyncio.CancelledError` and the eval cap's
       `CapReached` (`evalrun/budget.py`, an `Exception`) always propagate, wherever they come
       from, `tools` included. No logging (the transcript is customer text and tool output), no
       clock, no randomness, no database except through `tools`. The turn timeout belongs to the
       service (`turn_timeout`), not the loop.

    TODO (shesha, H1): write the loop. The guide's §2 explains each rule; §4 maps the tests to them.
    """
    raise NotImplementedError("H1 (M5): write run_turn (guide: docs/learning/m5-agent-loop.md)")

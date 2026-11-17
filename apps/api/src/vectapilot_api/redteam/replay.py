"""The scripted replay that stands in for M5's H1 `run_turn` in the red-team harness (M7 PR 2;
scoring rules, "The loop is a replay until M5's H1 exists").

M5's `run_turn` is shesha's `[HAND-WRITE]` and still a stub (`agent/loop.py`). The harness passes
`replay_loop(steps)` through `AgentService`'s existing `loop=` parameter instead. **It is not a
loop** (rule 2, like `tests/agent/fakes.py`' stand-ins): it plays a case's frozen
`scripted_model` steps in order, fixed tool calls and then a fixed ending, and never reads a step
the model returns or an observation to choose what to do next. It restates none of H1's rules:
what it plays is fixed before the turn (`playable`), and a script that would need one of those
rules (a refused write to observe, a step cap to reach) is a fixture error.
- **What it plays is static.** `playable` keeps the agent's steps (never `answer`); for the
  read-only service (G13: reads and hand-off only) it also leaves out every write-tool step, by
  the platform registry's kind, before the turn (leo's PR 2 review: a scripted write the
  read-only schema refuses would use up the fake's next reply in the gateway's repair). The fake
  provider is scripted with the same steps (`step_replies`, same `read_only`).
- **Each step first asks the step model** (`step_model.next_step`), so the fake provider receives
  the call and the guards see the model's text where M7 checks it; the answer is not read. An
  exception from the step model (the gateway refusing) propagates to the service, which hands
  off as it does for `run_turn`.
- **A read step** runs the tool and adds the envelope's observation to the transcript the next
  model call sees. **A write step** proposes and ends the turn with `ConfirmationRequired` (W1:
  the attacker's best case). The proposal is the one envelope fact the replay reads, because the
  outcome is made of it; a write the sandbox refuses raises `ScriptError` (the fixture does not
  fit its seed), never an observation to play on.
- **`reply` and `handoff`** end the turn with their text or reason.
- **Checked when built:** a script ends in a reply, a hand-off or a write (on the read-only
  service, a reply or a hand-off); a script longer than the turn's `limits.max_steps` raises
  `ScriptError` before the first model call. `tests/redteam/test_fixtures.py` checks every frozen
  script both ways.
`answer` steps are not the loop's: they script the answer route's model (`answer_texts`).

Every summary says "agent loop: scripted replay standing in for M5 H1 `run_turn`"
(`runinfo.REPLAY_LINE`). After M5's H1, a second dry pass runs the real loop with the same
scripted steps (D8 (i), `pending:M5-H1`).
"""

import json
from collections.abc import Callable, Mapping, Sequence

from vectapilot_api.agent.types import (
    ConfirmationRequired,
    LoopLimits,
    Observation,
    StepModel,
    ToolRunner,
    Transcript,
    TurnHandoff,
    TurnLoop,
    TurnOutcome,
    TurnReply,
)
from vectapilot_api.redteam.cases import (
    AnswerStep,
    CallToolStep,
    Case,
    HandoffStep,
    ReplyStep,
    ScriptStep,
)
from vectapilot_api.tools.registry import REGISTRY
from vectapilot_api.tools.types import ToolKind

AgentScriptStep = CallToolStep | ReplyStep | HandoffStep


class ScriptError(ValueError):
    """A script the replay cannot play as frozen (module docstring): a fixture error, never an
    outcome. The message names the rule, never a step's text."""


def agent_steps(case: Case) -> tuple[AgentScriptStep, ...]:
    """The case's steps the agent's step model plays (every one but `answer`)."""
    return tuple(step for step in case.fixture.scripted_model if not isinstance(step, AnswerStep))


def answer_texts(case: Case) -> tuple[str, ...]:
    """The answer texts the answer route's model plays (`answer` steps)."""
    return tuple(s.text for s in case.fixture.scripted_model if isinstance(s, AnswerStep))


def is_write(step: AgentScriptStep) -> bool:
    """A call of a write tool, by the platform registry's kind (never the runner's answer)."""
    return isinstance(step, CallToolStep) and REGISTRY[step.tool].kind is ToolKind.WRITE


def playable(
    steps: Sequence[ScriptStep], *, read_only: bool = False
) -> tuple[AgentScriptStep, ...]:
    """The steps the replay plays (module docstring). Raises `ScriptError` on an `answer` step,
    an empty script, or one that does not end the turn."""
    if any(isinstance(step, AnswerStep) for step in steps):
        raise ScriptError("an answer step scripts the answer route, not the agent loop")
    script = tuple(step for step in steps if not isinstance(step, AnswerStep))
    if read_only:
        script = tuple(step for step in script if not is_write(step))
    if not script:
        raise ScriptError("a script plays at least one step")
    last = script[-1]
    if not isinstance(last, ReplyStep | HandoffStep) and not is_write(last):
        raise ScriptError(
            "a script ends in a reply, a hand-off or (not on the read-only service) a write"
        )
    return script


Fill = Callable[[str], str]
"""Fills a fixture text's `{{pii:...}}` placeholders (`harness.FilledCase.text`)."""


def _unchanged(text: str) -> str:
    return text


def _arguments(step: CallToolStep, fill: Fill) -> dict[str, object]:
    return {k: fill(v) if isinstance(v, str) else v for k, v in step.arguments.items()}


def step_json(step: AgentScriptStep, fill: Fill = _unchanged) -> dict[str, object]:
    """One step as the agent's structured output holds it (`{"step": ...}`), its texts filled."""
    payload: dict[str, object]
    match step:
        case CallToolStep(tool=tool):
            payload = {
                "action": "call_tool",
                "tool": tool.value,
                "arguments": _arguments(step, fill),
            }
        case ReplyStep(text=text):
            payload = {"action": "reply", "text": fill(text)}
        case HandoffStep(reason=reason):
            payload = {"action": "handoff", "reason": reason.value}
    return {"step": payload}


def step_replies(
    steps: Sequence[ScriptStep], fill: Fill = _unchanged, *, read_only: bool = False
) -> tuple[str, ...]:
    """The fake provider's replies, one JSON text per played step, in order."""
    script = playable(steps, read_only=read_only)
    return tuple(json.dumps(step_json(step, fill), ensure_ascii=False) for step in script)


def replay_loop(
    steps: Sequence[ScriptStep], fill: Fill = _unchanged, *, read_only: bool = False
) -> TurnLoop:
    """A `TurnLoop` that replays `steps` (module docstring), their texts filled by `fill`. Raises
    `ScriptError` when built on a script it cannot play."""
    script = playable(steps, read_only=read_only)

    async def loop(
        message: str, *, step_model: StepModel, tools: ToolRunner, limits: LoopLimits
    ) -> TurnOutcome:
        if len(script) > limits.max_steps:
            raise ScriptError("the script is longer than the turn's step cap")
        transcript = Transcript(message)
        for step in script:
            await step_model.next_step(transcript)
            match step:
                case ReplyStep(text=text):
                    return TurnReply(fill(text))
                case HandoffStep(reason=reason):
                    return TurnHandoff(reason)
                case CallToolStep(tool=tool):
                    args: Mapping[str, object] = _arguments(step, fill)
                    if is_write(step):
                        envelope = await tools.propose(tool, args)
                        if envelope.proposal is None:
                            raise ScriptError("the sandbox refused a scripted write")
                        return ConfirmationRequired(envelope.proposal)
                    envelope = await tools.run(tool, args)
                    observed = Observation(tool=tool, arguments=args, text=envelope.observation)
                    transcript = transcript.observe(observed)
        # `playable` guarantees the last step ends the turn.
        raise ScriptError("a script ends the turn")

    return loop

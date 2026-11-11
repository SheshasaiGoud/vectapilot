"""The dry run's scripted fake model (M5 PR 6; scoring rules, "Dry or real").

A dry run sends nothing anywhere: the agent's gateway runs for real (structured output, repair,
records) on `PlanProvider`, a provider that answers each step from a **fixture plan** read from a
file under `tests/` (default `apps/api/tests/tooleval/fixtures/dry-plans.json`), **never from the
dataset**: a plan is chosen for a scenario by `sha256("dry-plan:<scenario id>")` alone, so no
scenario's message or expectation can shape the answers, and a dry number can never look like a
result. Its answers mean nothing; they exercise the plumbing (reads, proposals, confirms, hand-offs,
counts).

**The plan file** (`PlanFile`): `{"synthetic": true, "plans": [[step, ...], ...]}`, each step one
`AgentStep` payload as the model would answer (`{"action": "call_tool", "tool": ..., "arguments":
{...}}`, `{"action": "reply", "text": ...}` or `{"action": "handoff", "reason": ...}`). A plan
that runs out answers a fixed reply. A step the business's schema refuses (a tool it does not
enable) is what a real model can do too: the gateway repairs once, then the loop hands off.
"""

import hashlib
import json
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vectapilot_api.evalset.corpus import describe_validation_error
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.gateway.providers.base import ProviderRequest, ProviderResponse
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply

DEFAULT_PLANS: Final = Path("apps", "api", "tests", "tooleval", "fixtures", "dry-plans.json")
"""Relative to the repository root."""

FALLBACK_STEP: Final[Mapping[str, object]] = {
    "action": "reply",
    "text": "DRY RUN: the scripted plan ended.",
}


class PlanFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    synthetic: Literal[True]
    plans: tuple[tuple[dict[str, object], ...], ...] = Field(min_length=1)


def load_plans(path: Path) -> tuple[PlanFile, str]:
    """The plan file and its SHA-256 (for the dry run's fingerprint). Raises `DatasetError`."""
    if not path.is_file():
        raise DatasetError(f"the dry run's plan file {path.name} is missing")
    data = path.read_bytes()
    try:
        plans = PlanFile.model_validate_json(data)
    except ValidationError as error:
        detail = describe_validation_error(error, "(plans)")
        raise DatasetError(f"{path.name} is not a valid plan file: {detail}") from None
    return plans, hashlib.sha256(data).hexdigest()


def plan_index(scenario_id: str, count: int) -> int:
    """Which plan a scenario gets: its id's hash, nothing else (module docstring)."""
    return int(hashlib.sha256(f"dry-plan:{scenario_id}".encode()).hexdigest(), 16) % count


class PlanProvider:
    """A `Provider` playing one plan per turn (`begin`), one step per request."""

    def __init__(self, plans: PlanFile) -> None:
        self._plans = plans.plans
        self._steps: deque[Mapping[str, object]] = deque()
        self.requests = 0

    def begin(self, scenario_id: str) -> int:
        """Start the scenario's plan; returns its index."""
        index = plan_index(scenario_id, len(self._plans))
        self._steps = deque(self._plans[index])
        return index

    def _next(self) -> Mapping[str, object]:
        return self._steps.popleft() if self._steps else FALLBACK_STEP

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        self.requests += 1
        text = json.dumps({"step": self._next()}, ensure_ascii=False)
        return await FakeProvider([FakeReply(text=text)]).send(request)

    async def aclose(self) -> None:
        """Nothing to release."""


def plan_steps(plans: PlanFile) -> Sequence[Sequence[Mapping[str, object]]]:
    return plans.plans

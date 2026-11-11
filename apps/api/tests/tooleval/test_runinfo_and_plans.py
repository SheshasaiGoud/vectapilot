"""Run folders, run.json, results lines (`tooleval/runinfo.py`) and the dry run's scripted model
(`tooleval/plans.py`). Unit."""

import json
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from tests.agent.fakes import NO_DATABASE, PAWS, SUNRISE
from tests.tooleval.results import result
from tests.tooleval.runjson import FINGERPRINT, T0, tool_run_info
from tests.tools.helpers import REPO_ROOT
from vectapilot_api.agent.steps import build_agent_step
from vectapilot_api.gateway.providers.base import ProviderRequest
from vectapilot_api.gateway.types import Message
from vectapilot_api.tooleval.plans import (
    DEFAULT_PLANS,
    FALLBACK_STEP,
    PlanProvider,
    load_plans,
    plan_index,
)
from vectapilot_api.tooleval.runinfo import (
    DRY_TOOL_REPORTS,
    TOOL_REPORTS,
    ResultLine,
    folder_name,
    run_dir,
)
from vectapilot_api.tools.executor import ToolExecutor

PLANS: Final = REPO_ROOT / DEFAULT_PLANS


def test_folder_names_and_places() -> None:
    assert folder_name(live=True, started=T0, split="dev") == "20261111T120000Z-live-agent-dev"
    assert folder_name(live=False, started=T0, split="test") == "20261111T120000Z-fake-agent-test"
    root = Path("/r")
    assert run_dir(root, "v0.1", "x", live=True) == root / TOOL_REPORTS / "v0.1" / "x"
    assert run_dir(root, "v0.1", "x", live=False) == root / DRY_TOOL_REPORTS / "v0.1" / "x"
    assert DRY_TOOL_REPORTS.parts[:3] == ("evals", "reports", "tmp")


def test_the_fingerprint_hash_is_canonical_and_moves_with_every_input() -> None:
    same = FINGERPRINT.model_copy()
    assert same.sha256() == FINGERPRINT.sha256()
    changed = FINGERPRINT.model_copy(update={"agent_prompt_version": "agent-step.2"})
    assert changed.sha256() != FINGERPRINT.sha256()
    limits = FINGERPRINT.limits.model_copy(update={"max_steps": 3})
    assert FINGERPRINT.model_copy(update={"limits": limits}).sha256() != FINGERPRINT.sha256()


def test_run_json_says_router_none_and_the_safety_floor_was_not_exercised() -> None:
    info = tool_run_info("t1-live-agent-dev")
    assert info.routing == "none"
    assert "safety floor was not exercised" in info.routing_note


def test_a_result_line_holds_codes_never_free_text() -> None:
    assert result("s-1", handoff_reason="turn_timeout", error="OutcomeUnknown").error
    with pytest.raises(ValidationError):
        result("s-1", handoff_reason="the model said: book it now")
    with pytest.raises(ValidationError):
        result("s-1", message="TEST-ONLY a message")  # no such field
    with pytest.raises(ValidationError):
        result("s-1", confirm_results=("Executed with note",))
    line = result("s-1")
    assert ResultLine.model_validate_json(line.model_dump_json()) == line


def test_the_fixture_plans_load_and_every_step_fits_some_business_schema() -> None:
    plans, sha = load_plans(PLANS)
    assert len(sha) == 64
    schemas = [build_agent_step(ToolExecutor(NO_DATABASE, c)) for c in (PAWS, SUNRISE)]
    for plan in plans.plans:
        for step in plan:
            fits = []
            for schema in schemas:
                try:
                    schema.model_validate({"step": step})
                    fits.append(True)
                except ValidationError:
                    fits.append(False)
            assert any(fits), step
    assert "TEST-ONLY" not in PLANS.read_text("utf-8")


def test_a_plan_is_chosen_by_the_scenario_id_alone() -> None:
    assert plan_index("s-1", 8) == plan_index("s-1", 8)
    picks = {plan_index(f"s-{i}", 8) for i in range(200)}
    assert picks == set(range(8))


def request() -> ProviderRequest:
    return ProviderRequest(
        model="test-model",
        effort="low",
        max_tokens=100,
        system="TEST-ONLY system",
        messages=(Message(role="user", content="TEST-ONLY"),),
    )


@pytest.mark.anyio
async def test_the_plan_provider_plays_the_plan_then_a_fixed_reply() -> None:
    plans, _ = load_plans(PLANS)
    provider = PlanProvider(plans)
    index = provider.begin("s-1")
    answers = []
    for _ in range(len(plans.plans[index]) + 1):
        response = await provider.send(request())
        text = response.content[-1]
        answers.append(json.loads(text.text)["step"])  # type: ignore[union-attr]
    assert answers[:-1] == list(plans.plans[index])
    assert answers[-1] == FALLBACK_STEP
    assert provider.requests == len(answers)


def test_a_missing_or_invalid_plan_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing"):
        load_plans(tmp_path / "none.json")
    bad = tmp_path / "bad.json"
    bad.write_text('{"synthetic": false, "plans": []}', "utf-8")
    with pytest.raises(ValueError, match="not a valid plan file"):
        load_plans(bad)

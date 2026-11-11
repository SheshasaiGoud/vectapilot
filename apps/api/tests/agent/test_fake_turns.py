"""The plan's emergency cases under the eval harness's `router=none` (M5 PR 4, W11), through the
real service, the real step model on a fake provider and the real loop. No database.

Under `router=none` (`AgentService.respond_unrouted`) there is no W11 step before the loop: the
model must hand off itself, and the service must turn that `handoff(safety)` into an urgent
hand-off with the business's configured guidance (never model text) and no proposal. The fake
provider is scripted to hand off with reason `safety`, so these tests check the plumbing from the
model's JSON to the response, not that a real model hands off: that is the tool-call eval's job
(PR 6, live, "emergency plus booking", with "safety floor not exercised" in its report).

They run `run_turn`, shesha's M5 H1, so they wait for it through the one switch
(`pending_hand_write("run_turn")`, a strict xfail on NotImplementedError). The service turns any
exception into a hand-off, so each test passes `propagate=(NotImplementedError,)`: the stub's
error surfaces as the xfail expects, and once the loop is written nothing raises it.

The same cases with `agent_router = off` (fail closed before any model call) and `on` with a fake
router (urgent before any tool) are in test_service.py and test_route.py; with a stand-in loop
returning `handoff(safety)`, in test_service.py too.
"""

import pytest

from tests.agent.providers import BlockCheckingProvider, handoff_json
from tests.agent.test_service import EMERGENCIES, PROFILES, CannedAnswers, RecordingStore
from tests.answer.apps import gateway_for
from tests.canary.read_paths import pending_hand_write
from tests.intents.fakes import TRACE
from vectapilot_api.agent.responses import AgentRequest, HandoffReason, HandoffResponse
from vectapilot_api.agent.service import AgentService
from vectapilot_api.agent.types import LoopLimits, ModelHandoffReason, TurnHandoff
from vectapilot_api.intents.policy import Priority

pytestmark = [pytest.mark.anyio, *pending_hand_write("run_turn")]


@pytest.mark.parametrize(("business", "message"), EMERGENCIES)
async def test_under_router_none_a_loop_safety_handoff_is_urgent_with_guidance_never_a_proposal(
    business: str, message: str
) -> None:
    provider = BlockCheckingProvider([handoff_json("safety")])
    store = RecordingStore()
    service = AgentService(
        PROFILES,
        gateway=gateway_for(provider),
        store=store,
        answers=CannedAnswers(),
        limits=LoopLimits(max_steps=4),
        turn_timeout_s=35.0,
        propagate=(NotImplementedError,),  # the real loop (the default); its stub surfaces
    )

    run = await service.respond_unrouted(
        business, AgentRequest(message=message, customer_ref="cust-001"), trace_id=TRACE
    )

    assert isinstance(run.response, HandoffResponse), run.response
    assert (run.response.reason, run.response.priority) == (HandoffReason.SAFETY, Priority.URGENT)
    assert run.response.guidance == PROFILES[business].config.emergency_guidance  # separate lines
    assert run.outcome == TurnHandoff(ModelHandoffReason.SAFETY)
    assert store.created == [] and run.action is None  # no proposal, nothing to confirm
    assert run.steps == 1
    [blocks] = provider.seen
    assert [(block.kind, block.body) for block in blocks] == [("MESSAGE", message)]

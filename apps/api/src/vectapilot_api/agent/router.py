"""`POST /v1/agent` (M5 PR 4): one customer message → a reply, a proposed write waiting for its
confirmation, an answer or a hand-off.

The body is JSON, `{"message", "customer_ref"}`, closed: the message stays out of the URL, so no
proxy, browser history or access log ever holds it. The business comes from `X-Business-Id`
(ADR 0009), refused outside local, test and ci before the service runs, since `customer_ref` is
client-asserted until M8.

**Order of checks:** a body that is not JSON at all is FastAPI's own 422 → the business (400) →
the body's fields (422, echoing nothing) → the agent service (`agent/service.py`: W11, the router
when on, the loop, the turn timeout).

**Responses**, each 200 with `X-Trace-Id`: `reply`, `confirmation_required` (the one response that
carries a confirm token, W2), `handoff` or `answer`. Every failure inside the service is a
hand-off, never a 5xx: a customer always gets a next step, and a health business's emergency
guidance whenever W11 applies. This route only proposes: the write happens, at most once, through
`POST /v1/agent/actions/{id}/confirm` (`actions/router.py`), with the token.

It always runs the configured routing (`AgentService.respond`); the eval harness's `router=none`
(`respond_unrouted`) is never reachable from here (tests/agent/test_route.py).
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response

from vectapilot_api.agent.responses import AgentRequest, AgentResponse
from vectapilot_api.agent.service import AgentService
from vectapilot_api.business import get_business_id
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.retrieval.router import TRACE_HEADER
from vectapilot_api.retrieval.search import new_trace_id

router = APIRouter(prefix="/v1/agent", tags=["agent"])

_TRACE: Final = {
    TRACE_HEADER: {
        "description": "the turn's trace id, as in its `llm_call`, `tool_call` and `agent_turn`"
        " records",
        "schema": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    }
}
AGENT_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    200: {
        "description": "a reply, a proposal to confirm, an answer or a hand-off",
        "headers": _TRACE,
    },
    400: {"description": "a typed error: `{detail, error}`"},
}


def get_agent_service(request: Request) -> AgentService:
    service: AgentService = request.app.state.agent_service
    return service


@router.post("", responses=AGENT_RESPONSES)
async def agent(
    business_id: Annotated[BusinessId, Depends(get_business_id)],
    body: AgentRequest,
    service: Annotated[AgentService, Depends(get_agent_service)],
    response: Response,
) -> AgentResponse:
    """Handle one customer message for the business: reply, propose an action for the customer to
    confirm, answer from the business's documents, or hand off to staff."""
    trace_id = new_trace_id()
    run = await service.respond(business_id, body, trace_id=trace_id)
    response.headers[TRACE_HEADER] = trace_id
    return run.response

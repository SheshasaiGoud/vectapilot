"""`POST /v1/answer` (M2 PR 6): a cited answer, an honest "I don't know", or a hand-off label.

The body is JSON, `{"question": "..."}`: the question stays out of the URL, so no proxy, browser
history or access log ever holds it. The business comes from `X-Business-Id` (ADR 0009); the
reader is `customer` (approved public chunks only) until M8.

**Order of checks:** the business (400) → the body (422, echoing nothing) → the answer
(`AnswerService`: retrieve, abstain without a model call, or prompt, call and verify).

**Errors** are `KbError`s with fixed messages: the search's (`retrieval/router.SEARCH_ERRORS`)
plus the model's two. An answer the service cannot verify is never an error: it is the
weak-evidence outcome, with no text. Anything else propagates as a bug (500), H1's stub included.

**`X-Trace-Id`**, as on search (`retrieval/router.py`): made before the service runs and set on
the 200 and on every typed error, so a 502 or 503 can be joined to its `rag_answer`,
`embedding_call` and `llm_call` records. A 400 or the body's own 422 has none (nothing recorded).

**M7's guards (PR 8; G1, G11, G13)** run only with `guard_answer = "on"`: the route then calls
`guards.answer.AnswerGuard` (an object built in `create_app`, never a screening dependency) in
G1's order, with a separate `AnswerService` over the chunk-screening search and its own
`BudgetedGateway`. A guard's hand-off is `AnswerSafetyHandoff`, the other shape of this route's
response union; a full rate window is a 429. The service's own errors keep the table below. With
`"off"` (the default during M3's measurement window) the route is exactly M3's and M3's
`AnswerService` serves (tests/answer/test_answer_goldens.py: byte-identical).
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response

from vectapilot_api.answer.service import AnswerService
from vectapilot_api.answer.types import (
    AnswerFailed,
    AnswerRequest,
    AnswerResponse,
    ModelUnavailable,
)
from vectapilot_api.business import get_business_id
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.guards.answer import AnswerGuard, AnswerSafetyHandoff
from vectapilot_api.retrieval.router import (
    GUARDED_ROUTE_RESPONSES,
    SEARCH_ERRORS,
    TRACE_HEADER,
    ErrorTable,
    typed_error,
)
from vectapilot_api.retrieval.search import new_trace_id

router = APIRouter(prefix="/v1", tags=["answers"])

ANSWER_ERRORS: Final[ErrorTable] = {
    **SEARCH_ERRORS,
    ModelUnavailable: (503, "model_unavailable", "the answer model is unavailable; try later"),
    AnswerFailed: (502, "answer_failed", "the answer model failed"),
}


AnswerRouteResponse = AnswerResponse | AnswerSafetyHandoff
"""The route's response union (G11): `AnswerResponse` unchanged, or a guard's hand-off."""


def get_answer_service(request: Request) -> AnswerService:
    service: AnswerService = request.app.state.answer_service
    return service


def get_answer_guard(request: Request) -> AnswerGuard | None:
    """The route's guards (G13: an object the route calls); None while `guard_answer` is off."""
    guard: AnswerGuard | None = request.app.state.answer_guard
    return guard


def get_guarded_answer_service(request: Request) -> AnswerService | None:
    """The separate `AnswerService` the guards run (over the chunk-screening search and a
    `BudgetedGateway`); None while `guard_answer` is off."""
    service: AnswerService | None = request.app.state.guarded_answer_service
    return service


@router.post(
    "/answer",
    response_model=AnswerRouteResponse,
    responses=GUARDED_ROUTE_RESPONSES,
)
async def answer(
    business_id: Annotated[BusinessId, Depends(get_business_id)],
    body: AnswerRequest,
    service: Annotated[AnswerService, Depends(get_answer_service)],
    guard: Annotated[AnswerGuard | None, Depends(get_answer_guard)],
    guarded_service: Annotated[AnswerService | None, Depends(get_guarded_answer_service)],
    response: Response,
) -> AnswerResponse | AnswerSafetyHandoff | Response:
    """Answer the question from the business's approved public documents, citing them; or say
    honestly that the evidence is too weak (`dont_know`, or `handoff` if so configured)."""
    trace_id = new_trace_id()  # before the service: a typed error carries it too
    if guard is not None:  # guard_answer = "on" (module docstring)
        return await _guarded(business_id, body, trace_id, guard, guarded_service, response)
    try:
        run = await service.answer(business_id, body.question, trace_id=trace_id)
    except Exception as error:
        failure = typed_error(error, ANSWER_ERRORS, trace_id=trace_id)
        if failure is None:
            raise  # a bug, or H1's stub: never turned into a typed error
    else:
        response.headers[TRACE_HEADER] = trace_id
        return run.response
    raise failure  # outside the `except`: nothing (and no message) is chained to it


async def _guarded(
    business_id: str,
    body: AnswerRequest,
    trace_id: str,
    guard: AnswerGuard,
    service: AnswerService | None,
    response: Response,
) -> AnswerResponse | AnswerSafetyHandoff | Response:
    guarded = await guard.answer(business_id, body, trace_id=trace_id, service=service)
    if guarded.failure is not None:  # the service's own error: the same table as M3's route
        failure = typed_error(guarded.failure, ANSWER_ERRORS, trace_id=trace_id)
        if failure is None:
            raise guarded.failure  # a bug: never turned into a typed error
        raise failure
    if guarded.response is None:  # pragma: no cover (a guard always answers or fails)
        raise RuntimeError("the answer guard returned nothing")
    if isinstance(guarded.response, Response):  # a limit's 429, returned as built
        guarded.response.headers[TRACE_HEADER] = trace_id
        return guarded.response
    response.headers[TRACE_HEADER] = trace_id
    return guarded.response

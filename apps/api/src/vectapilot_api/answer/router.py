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
from vectapilot_api.retrieval.router import (
    ROUTE_RESPONSES,
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


def get_answer_service(request: Request) -> AnswerService:
    service: AnswerService = request.app.state.answer_service
    return service


@router.post("/answer", responses=ROUTE_RESPONSES)
async def answer(
    business_id: Annotated[BusinessId, Depends(get_business_id)],
    body: AnswerRequest,
    service: Annotated[AnswerService, Depends(get_answer_service)],
    response: Response,
) -> AnswerResponse:
    """Answer the question from the business's approved public documents, citing them; or say
    honestly that the evidence is too weak (`dont_know`, or `handoff` if so configured)."""
    trace_id = new_trace_id()  # before the service: a typed error carries it too
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

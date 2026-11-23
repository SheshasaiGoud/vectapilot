"""The document path's runner (M7 PR 7; `plan.CasePath.DOCUMENT`): category D through
`POST /v1/extractions` with the guard on (`guards.document`).

**The document** is the case's `fixture.document`, built into PDF bytes at run time
(`redteam.pdf.document_pdf`): its visible lines, then its hidden ones (`3 Tr`, white fill, off
the page), each line filled in memory only.

**Two environments, one guard.**
- `in_process` (d-002 to d-005): the route's own guard object, built as `create_app` builds it
  with `guard_routes = "on"` (`ExtractionGuard` over `RouteLimits` with an allowing in-memory
  `FakeLimiter`, the in-process world's guard folder, `redteam.agent.world_of`), called in the
  route's order: `admit` (before the body is read), `DocumentScreen.scan` (G13: after the
  extraction, before the store), then `finish` (the request's one `guard` record). M6's
  extraction service is not in the loop: the screen reads the bytes itself, as the route does.
- `database` (d-001, the surface's end-to-end case): the served app's real route. The bytes go
  as the business's first extraction kind; a 201 is stored by M6's store as the app role, and its
  id goes to the served suite (`stored_extractions`), which deletes it as the owner afterwards
  (the served suite's note: no row is left for a sample business).

**What a result holds** (`scoring.CaseResult`, codes only): the outcome (`allowed` when the
upload went through, its flags in the response; `unavailable_503` when the screen could not run,
G3: nothing stored), and the extraction route's `guard` record: `decided_by`, `error_type` and the
findings the scorer compares with `scoring.DOCUMENT_FINDINGS`. D has no model, so no model
control; a refused document (404, 413, 415) means the case's PDF was not built as the route takes
it, and the runner raises instead of scoring it (`VEHICLE_REFUSED`).

**While H1 is a stub** the screen fails closed (`DocumentScanFailed` from H1's
`NotImplementedError`): `unavailable_503` decided by `guard_error`, which `run_case` makes
`pending:H1` (`plan.needs`: a document case needs H1 only). d-005 (the forced review) stays a
known gap until PR 11: the screen flags a page, it never hands an upload off (G7).
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from vectapilot_api.guards.config import GuardConfig
from vectapilot_api.guards.document import DocumentScanFailed, ExtractionGuard
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.guards.match import match_terms
from vectapilot_api.guards.output import Matcher
from vectapilot_api.guards.routes import RouteLimits
from vectapilot_api.guards.types import DecidedBy, Finding, RouteSlug
from vectapilot_api.redteam.agent import TRACE_ID, guard_records, world_of
from vectapilot_api.redteam.cases import Case
from vectapilot_api.redteam.pdf import document_pdf
from vectapilot_api.redteam.scoring import CaseResult

if TYPE_CHECKING:
    # Annotations only: `harness` imports this module to register the runner.
    from vectapilot_api.redteam.harness import FilledCase, RunEnv, Runner

EXTRACTION_PATH: Final = "/v1/extractions"
BUSINESS_HEADER: Final = "X-Business-Id"
PDF: Final = "application/pdf"
VEHICLE_REFUSED: Final = "the document case's PDF was refused before the screen"
"""Why the runner raises (module docstring): a fixed text, never the case's."""

STATUS_OUTCOMES: Final[Mapping[int, str]] = MappingProxyType(
    {201: "allowed", 422: "rejected_422", 429: "blocked_429", 503: "unavailable_503"}
)
"""The route's status as the case's outcome; any other 5xx is `server_error`, any other 4xx
raises (`VEHICLE_REFUSED`)."""


class _Response(Protocol):
    status_code: int

    def json(self) -> Any: ...


class _Client(Protocol):
    def post(
        self,
        url: str,
        *,
        content: bytes,
        params: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> _Response: ...


def extraction_record(records: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    """The one `guard` record of the extraction route among `records`, or None."""
    found = [r for r in records if r.get("route") == RouteSlug.EXTRACTION.value]
    if len(found) > 1:
        raise ValueError("one upload wrote more than one extraction guard record")
    return found[0] if found else None


def result_of(case: Case, outcome: str, record: Mapping[str, object] | None) -> CaseResult:
    """The case's result from its outcome and its `guard` record (codes only)."""
    decided = None if record is None else record.get("decided_by")
    error_type = None if record is None else record.get("error_type")
    findings = None if record is None else record.get("findings")
    return CaseResult(
        case.id,
        outcome,
        DecidedBy(decided) if isinstance(decided, str) else None,
        error_type=error_type if isinstance(error_type, str) else None,
        findings=frozenset(Finding(code) for code in findings)
        if isinstance(findings, dict)
        else frozenset(),
    )


def outcome_of(status: int) -> str:
    if status in STATUS_OUTCOMES:
        return STATUS_OUTCOMES[status]
    if status >= 500:
        return "server_error"
    raise ValueError(VEHICLE_REFUSED)


# ── In process ───────────────────────────────────────────────────────────────────────────────


def extraction_guard(guards: GuardConfig, matcher: Matcher) -> ExtractionGuard:
    """The route's guard as `create_app` builds it (module docstring), on an allowing fake
    limiter: the limits are category K's subject, never D's."""
    limits = RouteLimits(
        route=RouteSlug.EXTRACTION, config=guards, limiter=FakeLimiter(), types={}, guidance={}
    )
    return ExtractionGuard(limits=limits, config=guards, matcher=matcher)


async def screened(case: Case, data: bytes, guard: ExtractionGuard) -> CaseResult:
    """`admit`, `scan` and `finish`, in the route's order (module docstring)."""
    with guard_records() as records:
        admitted = await guard.admit(case.business_id, trace_id=TRACE_ID)
        if isinstance(admitted, JSONResponse):
            outcome = outcome_of(admitted.status_code)
        else:
            try:
                await admitted.scan(data, reader=None)
                outcome = "allowed"
            except DocumentScanFailed:
                outcome = "unavailable_503"
            finally:
                admitted.finish()
    return result_of(case, outcome, extraction_record(records))


# ── Through the served app (the database environment) ───────────────────────────────────────


@dataclass(frozen=True)
class Served:
    """What the database environment needs of the served suite (`tests/redteam/served.py`)."""

    app: FastAPI
    client: _Client
    stored: list[tuple[str, str]]
    """(business id, extraction id) of every extraction a case stored: the suite deletes them."""


def served_of(env: "RunEnv") -> Served:
    served = env.app
    app = getattr(served, "app", None)
    client = getattr(served, "client", None)
    stored = getattr(served, "stored_extractions", None)
    if env.environment != "database" or not isinstance(app, FastAPI) or client is None:
        raise ValueError("the database environment needs the served app")
    if not isinstance(stored, list):
        raise ValueError("the served suite must take back the extractions a case stores")
    return Served(app, cast(_Client, client), stored)


def first_kind(app: FastAPI, business_id: str) -> str:
    """The business's first extraction kind on the served app (its extraction folder)."""
    extraction = app.state.extraction
    loaded = None if extraction is None else extraction.businesses.get(business_id)
    kind = None if loaded is None else next(iter(loaded.kinds), None)
    if kind is None:
        raise ValueError("the served app has no extraction kind for the case's business")
    return str(kind)


def through_the_route(case: Case, data: bytes, env: "RunEnv") -> CaseResult:
    served = served_of(env)
    with guard_records() as records:
        response = served.client.post(
            EXTRACTION_PATH,
            content=data,
            params={"kind": first_kind(served.app, case.business_id)},
            headers={BUSINESS_HEADER: case.business_id, "Content-Type": PDF},
        )
    body = response.json()
    if response.status_code == 201 and isinstance(body, dict) and isinstance(body.get("id"), str):
        served.stored.append((case.business_id, body["id"]))
    return result_of(case, outcome_of(response.status_code), extraction_record(records))


# ── The runner ───────────────────────────────────────────────────────────────────────────────


def document_runner(matcher: Matcher = match_terms) -> "Runner":
    """The document path's runner over `matcher` (H1 when registered; tests pass a stand-in).
    The database environment uses the served app's own matcher (`create_app`'s)."""

    def run(filled: "FilledCase", env: "RunEnv") -> CaseResult:
        case = filled.case
        if case.fixture.document is None:
            raise ValueError(f"case {case.id}: a document case needs its document")
        data = document_pdf(case.fixture.document, filled.text)
        if env.environment == "database":
            return through_the_route(case, data, env)
        if env.dataset_dir is None:
            raise ValueError("the in-process document path needs the dataset folder")
        guard = extraction_guard(world_of(env.dataset_dir).guards, matcher)
        return asyncio.run(screened(case, data, guard))

    return run


run_document: Final = document_runner()
"""The registered runner (`harness.RUNNERS`)."""

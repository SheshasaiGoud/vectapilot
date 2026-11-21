"""The log path's runner (M7 PR 6; `plan.CasePath.LOG`): F's forced exceptions on every route,
through the served app's own formatter (`guards.redact.RedactingJsonFormatter`, G10).

**The vehicle.** For one case, the route's service dependency is answered by a stand-in that raises
`RuntimeError` whose message holds the case's filled input (generated PII), the worst a bug can do:
the agent's guard (an exception the guard did not catch), the answer service, the search service,
the extraction service and the confirm service. The request is sent through the served suite's
client, shaped to pass the route's own validation (confirm's token has `actions.tokens`' length),
so the forced exception is raised; a request that never reaches its stand-in is a broken vehicle,
never a pass: the runner raises (`VEHICLE_NOT_REACHED`). Where the route catches it (search,
extraction, confirm's typed errors), the route's own lines are what is checked; where it escapes
(the served client re-raises), the runner writes the server's error line itself, as uvicorn's
`run_asgi` would ("Exception in ASGI application" with `exc_info`,
tests/guards/test_log_canary.py's `Server`), and checks it too.

**The check.** Every record written during the request, any logger, is formatted with the root
handler's formatter, the one `create_app` installed. No form of any generated value
(`redteam.pii.leak_forms`: raw, percent-encoded, JSON-escaped) may appear in any line: `no_leak`,
else `leak`. Codes only: the runner never returns or logs a line.

**A formatter without a masker** (`RedactingJsonFormatter.masker is None`, the interim while H2
was the stub, decided 2026-11-18): the runner reports the stub (`guard_error`,
`NotImplementedError`) instead of scoring an interim formatter, so `run_case` makes the case
`pending:H2` while H2's probe says unwritten, and a plain miss once it says written. H2 is written
(2026-11-20): `create_app` installs `redact.log_masker()`, and F's logs are scored.
"""

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final, NoReturn, cast
from uuid import UUID

from fastapi import FastAPI

from vectapilot_api.actions.tokens import TOKEN_LENGTH
from vectapilot_api.guards.redact import RedactingJsonFormatter
from vectapilot_api.guards.types import DecidedBy
from vectapilot_api.redteam.cases import Route
from vectapilot_api.redteam.pii import PLACEHOLDER, PiiType, Placeholder, leak_forms
from vectapilot_api.redteam.scoring import CaseResult

if TYPE_CHECKING:
    from vectapilot_api.redteam.harness import FilledCase, RunEnv

ASGI_ERROR: Final = "Exception in ASGI application"
BUSINESS_HEADER: Final = "X-Business-Id"
CLIENT_LOGGERS: Final = ("httpx2",)
"""The test client's own loggers (the client side, not the API)."""
CONFIRM_TOKEN: Final = "t" * TOKEN_LENGTH
"""A token of the right shape (the route validates it before the service); the stand-in confirm
service never reads it."""
VEHICLE_NOT_REACHED: Final = "the log case's request never reached its raising stand-in"
"""Why the runner raises (module docstring): a fixed text, never the case's."""
ACTION_ID: Final = UUID(int=7)
PDF_BODY: Final = b"%PDF-1.4\n% TEST-ONLY: the extraction stand-in never reads it\n"


class _Raising:
    """Any service: every attribute is a method that raises the case's forced exception."""

    def __init__(self, message: str) -> None:
        self._message = message
        self.raised = 0

    def __getattr__(self, name: str) -> Callable[..., Any]:
        message = self._message

        async def raising(*args: object, **kwargs: object) -> NoReturn:
            self.raised += 1
            raise RuntimeError(message)

        return raising


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith(CLIENT_LOGGERS):
            self.records.append(record)


@contextmanager
def _captured() -> Iterator[_Records]:
    root = logging.getLogger()
    handler = _Records()
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)


def app_formatter() -> logging.Formatter | None:
    """The root handler's formatter, as `create_app`'s `configure_logging` installed it."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler.formatter, RedactingJsonFormatter):
            return handler.formatter
    return None


def generated_values(filled: "FilledCase") -> list[str]:
    """Every value the case's placeholders were filled with (same case id, type and n)."""
    values = []
    for match in PLACEHOLDER.finditer(filled.case.input):
        kind, n = PiiType(match.group(1)), int(match.group(2) or 1)
        values.append(filled.generator.value(filled.case.id, Placeholder(kind, n)))
    return values


def _dependency(route: Route) -> Callable[..., Any]:
    from vectapilot_api.actions.router import get_confirm_service
    from vectapilot_api.agent.router import get_agent_guard
    from vectapilot_api.answer.router import get_answer_service
    from vectapilot_api.extraction.router import get_extraction_service
    from vectapilot_api.retrieval.router import get_search_service

    dependencies: Mapping[Route, Callable[..., Any]] = {
        Route.AGENT: get_agent_guard,
        Route.ANSWER: get_answer_service,
        Route.SEARCH: get_search_service,
        Route.EXTRACTION: get_extraction_service,
        Route.CONFIRM: get_confirm_service,
    }
    return dependencies[route]


def _send(client: Any, route: Route, business: str, text: str, kind: str | None) -> object:
    headers = {BUSINESS_HEADER: business}
    if route is Route.AGENT:
        body = {"message": text, "customer_ref": "cust-001"}
        return client.post("/v1/agent", json=body, headers=headers)
    if route is Route.ANSWER:
        return client.post("/v1/answer", json={"question": text}, headers=headers)
    if route is Route.SEARCH:
        return client.get("/v1/kb/search", params={"q": text}, headers=headers)
    if route is Route.EXTRACTION:
        return client.post(
            "/v1/extractions",
            params={"kind": kind or "unknown"},
            content=PDF_BODY,
            headers=headers | {"content-type": "application/pdf"},
        )
    return client.post(
        f"/v1/agent/actions/{ACTION_ID}/confirm",
        json={"customer_ref": "cust-001", "confirm_token": CONFIRM_TOKEN},
        headers=headers,
    )


def _first_kind(app: FastAPI, business: str) -> str | None:
    extraction = app.state.extraction
    loaded = None if extraction is None else extraction.businesses.get(business)
    return None if loaded is None else next(iter(loaded.kinds), None)


def run_logs(filled: "FilledCase", env: "RunEnv") -> CaseResult:
    """The registered runner (module docstring)."""
    case = filled.case
    served = env.app
    app = getattr(served, "app", None)
    client = getattr(served, "client", None)
    if env.environment != "database" or not isinstance(app, FastAPI) or client is None:
        raise ValueError("the log path runs in the database environment")
    formatter = app_formatter()
    if formatter is None:
        raise ValueError("the served app's redacting formatter is not installed")
    if cast(RedactingJsonFormatter, formatter).masker is None:  # no masker (module docstring)
        return CaseResult(
            case.id, "handoff", DecidedBy.GUARD_ERROR, error_type=NotImplementedError.__name__
        )
    dependency = _dependency(case.route)
    stand_in = _Raising(f"forced failure: {filled.input}")
    app.dependency_overrides[dependency] = lambda: stand_in
    escaped: BaseException | None = None
    try:
        with _captured() as captured:
            try:
                _send(
                    client,
                    case.route,
                    case.business_id,
                    filled.input,
                    _first_kind(app, case.business_id),
                )
            except Exception as error:  # the served client re-raises what escaped the app
                escaped = error
            if escaped is not None:
                logging.getLogger("uvicorn.error").error(f"{ASGI_ERROR}\n", exc_info=escaped)
    finally:
        app.dependency_overrides.pop(dependency, None)
    if not stand_in.raised:
        raise ValueError(VEHICLE_NOT_REACHED)
    lines = [formatter.format(record) for record in captured.records]
    forms = {form for value in generated_values(filled) for form in leak_forms(value)}
    leaked = any(form in line for line in lines for form in forms)
    return CaseResult(case.id, "leak" if leaked else "no_leak", reached=bool(lines))

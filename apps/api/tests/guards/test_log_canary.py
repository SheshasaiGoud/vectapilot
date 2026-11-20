"""G10's global log canary (M7 PR 1): a synthetic PII canary goes through every route, in its
success, refusal and forced-exception paths, and no log line the API writes may hold any part of
it. Unit: every route runs on the stand-ins its own route tests use (no database, no Redis).

**What is checked is the formatted line**, never `caplog`'s raw record: each record is formatted
by the app's own formatter (the one `configure_logging(..., redact=True)` installed on the root
logger: PR 3's `RedactingJsonFormatter`), `exc_info` included, and the canary is searched in the
line as written and URL-decoded.

**The server is part of the log.** In production uvicorn writes two lines of its own per request:
the access line (`uvicorn.access`, which `DropQueryString` cuts at the `?`) and, when an exception
escapes the app, "Exception in ASGI application" with `exc_info` (`uvicorn.error`,
`protocols/http/h11_impl.py`, `run_asgi`). `TestClient` writes neither, so `Server` below writes
both as uvicorn does.

**The two server lines PR 1 pinned as known gaps are hard assertions since PR 3:**
- **G10-S1** (11 cases, one per route in `ESCAPES`): a forced exception that escapes a route was
  written by the server's error line with its message, canary included. Decided 2026-11-16
  (shesha, D-error-line): the `uvicorn.error` record's `exception` is the exception's class name
  and stack frames, never its message (`guards.redact.frames_only`), and masked once H2 exists.
  No canary part at all is left.
- **G10-S2** (2 cases, diego's PR 1 review): customer text in the URL *path* (a malformed
  `{action_id}` on the confirm route, an unknown route's 404) reached the access line, because
  `DropQueryString` cuts only the query. [proposal] The access line names the matched route's
  template, `[unmatched]` for a 404 (`guards.redact.AccessLine`), so no canary part at all is
  left, not only the PII ones.

The canary is synthetic: the phone number is not dialable (+91 numbers never start with 0, as
the repository's other fixtures) and the e-mail is under `example.com` (RFC 2606).
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, Literal, NoReturn
from urllib.parse import quote, unquote_plus

import httpx2
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tests.actions.fakes import ScriptedSource
from tests.actions.test_confirm_route import EXECUTED, StandIn
from tests.actions.test_confirm_route import PATH as CONFIRM_PATH
from tests.agent.fakes import PAWS, canned_loop
from tests.agent.providers import BlockCheckingProvider, reply_json
from tests.agent.test_service import ALL, RETAIL, CannedAnswers, RecordingStore
from tests.answer.apps import answer_service, gateway_for
from tests.answer.providers import CitingProvider
from tests.conftest import make_settings
from tests.extraction.fakes import SUNRISE_ID, referral
from tests.extraction.helpers import EXTRACTION_DIR
from tests.extraction.served import RecordingStore as ExtractionStore
from tests.extraction.served import gateway_on
from tests.extraction.standins import (
    REFERRAL_ANSWER,
    REFERRAL_LINES,
    accept_everything,
    pdf,
    reply,
    with_changes,
)
from tests.guards.helpers import sample_guard_dir, use_answer_key_selftest
from tests.kb.test_routes import PARAMS as KB_PARAMS
from tests.kb.test_routes import build_kb
from tests.retrieval.standins import build_search, hit
from tests.tools.helpers import CORPUS_DIR, SANDBOX_DIR
from vectapilot_api import health, main
from vectapilot_api.actions.confirm import ConfirmService
from vectapilot_api.agent.profiles import build_profile
from vectapilot_api.agent.router import get_agent_service
from vectapilot_api.agent.service import AgentService
from vectapilot_api.agent.types import (
    LoopLimits,
    StepModel,
    ToolRunner,
    Transcript,
    TurnLoop,
    TurnOutcome,
    TurnReply,
)
from vectapilot_api.answer.router import get_answer_service
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.config import Settings
from vectapilot_api.db import Database
from vectapilot_api.extraction.extractor import FieldExtractor
from vectapilot_api.extraction.service import ExtractionService
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.guards.redact import METADATA_RECORDS, UNMATCHED, RedactingJsonFormatter
from vectapilot_api.guards.selftest import SelftestStatus
from vectapilot_api.health import CheckResult
from vectapilot_api.intents.policy import BusinessType
from vectapilot_api.log import JsonFormatter
from vectapilot_api.main import create_app
from vectapilot_api.retrieval.router import get_search_service
from vectapilot_api.tools.config import ToolName

MARK: Final = "zq7canary4e1b"
"""Free text with no PII shape: found anywhere, it means customer text reached a log."""
PHONE: Final = "+91 00000 20777"
EMAIL: Final = "m7.canary@example.com"
CANARY: Final = f"note {MARK}: call {PHONE} or write to {EMAIL}"
PII_PARTS: Final = ("00000 20777", "0000020777", EMAIL)
"""The phone (spaced, and as bare digits) and the e-mail: what PR 3's masking must remove."""
FORCED: Final = f"forced failure: {CANARY}"
"""Every forced exception's message."""

ASGI_ERROR: Final = "Exception in ASGI application"
CLIENT_LOGGERS: Final = ("httpx2",)
"""The test client's own loggers: they log the request URL on the *client* side, not the API."""
BUSINESS: Final = "clinic-a"
Kind = Literal["success", "refusal", "forced"]
KINDS: Final[tuple[Kind, ...]] = ("success", "refusal", "forced")


def parts_in(line: str) -> list[str]:
    """Every canary part in `line`, as written or URL-decoded (an access line or a URL)."""
    seen = (line, unquote_plus(line))
    return sorted({part for part in (MARK, *PII_PARTS) if any(part in text for text in seen)})


# ── The server's own lines, as uvicorn writes them ───────────────────────────────────────────


class Server:
    """What uvicorn does around the app, for logging only: one access line per request (with the
    query string, which `DropQueryString` cuts) and, when an exception escapes, its error line
    with `exc_info` (`h11_impl.run_asgi`). The exception is re-raised for `TestClient`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 500

        async def sending(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, sending)
        except BaseException as exc:
            logging.getLogger("uvicorn.error").error(f"{ASGI_ERROR}\n", exc_info=exc)
            raise
        finally:
            path = quote(scope["path"])
            if scope["query_string"]:
                path += "?" + scope["query_string"].decode("ascii")
            logging.getLogger("uvicorn.access").info(
                '%s - "%s %s HTTP/%s" %d',
                "127.0.0.1:50000",
                scope["method"],
                path,
                scope["http_version"],
                status,
            )


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@dataclass
class Exchange:
    """One request on one app: `started` runs after startup (state set by the lifespan is
    replaced there, as the route tests do), `send` makes the request."""

    app: FastAPI
    send: Callable[[TestClient], httpx2.Response]
    status: int
    started: Callable[[FastAPI], None] = field(default=lambda app: None)


@dataclass
class Logged:
    response: httpx2.Response
    lines: list[str]
    """Every line but the server's escaped-exception line, formatted by the app's formatter."""
    server_errors: list[str]
    access: list[str]
    """The server's access lines (also in `lines`)."""
    records: list[logging.LogRecord]
    """The raw records behind every line (for the detector's own positive control only)."""


def run(exchange: Exchange) -> Logged:
    root = logging.getLogger()
    [handler] = root.handlers  # `configure_logging`'s, installed by `create_app`
    formatter = handler.formatter
    assert isinstance(formatter, RedactingJsonFormatter)  # the app's own (PR 3)
    capture = Capture()
    root.addHandler(capture)
    try:
        with TestClient(
            Server(exchange.app),
            raise_server_exceptions=False,
            headers={"User-Agent": CANARY},
        ) as client:
            exchange.started(exchange.app)
            response = exchange.send(client)
    finally:
        root.removeHandler(capture)
    records = [r for r in capture.records if not r.name.startswith(CLIENT_LOGGERS)]
    escaped = [r for r in records if r.name == "uvicorn.error" and ASGI_ERROR in r.getMessage()]
    return Logged(
        response,
        [formatter.format(r) for r in records if r not in escaped],
        [formatter.format(r) for r in escaped],
        [formatter.format(r) for r in records if r.name == "uvicorn.access"],
        records,
    )


def raising(*args: Any, **kwargs: Any) -> NoReturn:
    raise RuntimeError(FORCED)


# ── The app, as production configures it (every folder set), on stand-ins ────────────────────


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return make_settings(
        tools_config_dir=SANDBOX_DIR,
        extraction_config_dir=EXTRACTION_DIR,
        corpus_dir=CORPUS_DIR,
        guard_config_dir=sample_guard_dir(tmp_path_factory.mktemp("canary") / "guard"),
        log_level="INFO",
    )


@pytest.fixture(autouse=True)
def registered(monkeypatch: pytest.MonkeyPatch) -> None:
    async def business_exists(self: Database, business_id: str) -> bool:
        return True

    monkeypatch.setattr(Database, "business_exists", business_exists)
    # The startup self-test on the answer-key stand-in, never H1: a half-written H1 must not turn
    # this hard gate red for a reason that is not a leak (diego's PR 1 review).
    use_answer_key_selftest(monkeypatch)


Builder = Callable[[Kind, Settings, pytest.MonkeyPatch], Exchange]


def get(path: str) -> Callable[[TestClient], httpx2.Response]:
    return lambda client: client.get(path, params={"note": CANARY})


def post_text(path: str) -> Callable[[TestClient], httpx2.Response]:
    return lambda client: client.post(path, content=CANARY.encode())


async def failing_endpoint() -> dict[str, str]:
    raise RuntimeError(FORCED)


def healthz(kind: Kind, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Exchange:
    if kind == "forced":
        # The endpoint itself fails (it reads no input, so there is nothing deeper to break).
        # FastAPI builds an included route's handler from the route's endpoint per app, so the
        # endpoint is replaced before the app is built.
        [route] = [
            r for r in health.router.routes if isinstance(r, APIRoute) and r.path == "/healthz"
        ]
        monkeypatch.setattr(route, "endpoint", failing_endpoint)
    app = create_app(settings)
    if kind == "refusal":
        return Exchange(app, post_text("/healthz"), 405)
    return Exchange(app, get("/healthz"), 500 if kind == "forced" else 200)


def readyz(kind: Kind, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Exchange:
    app = create_app(settings)
    if kind == "refusal":  # the real checks: Postgres and Redis are unreachable (port 1)
        return Exchange(app, get("/readyz"), 503)

    async def ok() -> CheckResult:
        return CheckResult("ok", ok=True)

    async def broken() -> CheckResult:
        raise RuntimeError(FORCED)

    check = broken if kind == "forced" else ok
    app.dependency_overrides[health.get_readiness_checks] = lambda: {"postgres": check}
    return Exchange(app, get("/readyz"), 503 if kind == "forced" else 200)


def docs_page(path: str, function: str) -> Builder:
    def build(kind: Kind, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Exchange:
        app = create_app(settings)
        if kind == "refusal":
            return Exchange(app, post_text(path), 405)
        if kind == "forced":
            if function == "openapi":
                monkeypatch.setattr(app, "openapi", raising)
            else:
                monkeypatch.setattr(f"fastapi.applications.{function}", raising)
            return Exchange(app, get(path), 500)
        return Exchange(app, get(path), 200)

    return build


KB_DOCUMENT: Final = f"# Opening hours\n\nWe are open 9 to 5. {CANARY}\n".encode()


def kb_documents(kind: Kind, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Exchange:
    app, store, _, _ = build_kb(settings, monkeypatch)
    content_type = "application/json" if kind == "refusal" else "text/markdown"
    if kind == "forced":
        store.fail = RuntimeError(FORCED)

    def send(client: TestClient) -> httpx2.Response:
        headers = {BUSINESS_HEADER: BUSINESS, "Content-Type": content_type}
        return client.post(
            "/v1/kb/documents", content=KB_DOCUMENT, params=KB_PARAMS, headers=headers
        )

    return Exchange(app, send, {"success": 201, "refusal": 415, "forced": 500}[kind])


def kb_reindex(kind: Kind, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Exchange:
    app, store, _, _ = build_kb(settings, monkeypatch)
    if kind == "forced":
        store.fail = RuntimeError(FORCED)
    business = f"Not-A-Business {MARK}" if kind == "refusal" else BUSINESS

    def send(client: TestClient) -> httpx2.Response:
        return client.post(
            "/v1/kb/reindex", params={"note": CANARY}, headers={BUSINESS_HEADER: business}
        )

    return Exchange(app, send, {"success": 200, "refusal": 400, "forced": 500}[kind])


def search_app(settings: Settings, kind: Kind, provider: CitingProvider | None = None) -> FastAPI:
    hits = {BUSINESS: [hit(f"We are open 9 to 5. {CANARY}", 0.9)]}
    search = build_search(hits, raises=RuntimeError(FORCED) if kind == "forced" else None)
    app = create_app(settings)
    app.dependency_overrides[get_search_service] = lambda: search.service
    answer = answer_service(search, provider or CitingProvider(answer=f"Open 9 to 5. {CANARY}"))
    app.dependency_overrides[get_answer_service] = lambda: answer
    return app


def kb_search(kind: Kind, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Exchange:
    params = {"q": CANARY, "k": "0" if kind == "refusal" else "5"}

    def send(client: TestClient) -> httpx2.Response:
        return client.get("/v1/kb/search", params=params, headers={BUSINESS_HEADER: BUSINESS})

    status = {"success": 200, "refusal": 422, "forced": 500}[kind]
    return Exchange(search_app(settings, kind), send, status)


def answer(kind: Kind, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Exchange:
    question = CANARY * 60 if kind == "refusal" else CANARY  # over 2,000 characters: a 422

    def send(client: TestClient) -> httpx2.Response:
        return client.post(
            "/v1/answer",
            json={"question": question},
            params={"note": CANARY},
            headers={BUSINESS_HEADER: BUSINESS},
        )

    status = {"success": 200, "refusal": 422, "forced": 500}[kind]
    return Exchange(search_app(settings, kind), send, status)


def step_and_tool_loop(result: TurnOutcome) -> TurnLoop:
    """A stand-in for `run_turn` (not a loop) for the success path: it asks the step model once,
    then runs one read tool with the canary as its argument, so the agent gateway's `llm_call`
    record and the executor's `tool_call` record are written with customer text in play."""

    async def loop(
        message: str, *, step_model: StepModel, tools: ToolRunner, limits: LoopLimits
    ) -> TurnOutcome:
        await step_model.next_step(Transcript(message))
        await tools.run(ToolName.ORDER_STATUS, {"order_id": CANARY})  # invalid_arguments
        return result

    return loop


def agent(kind: Kind, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Exchange:
    app = create_app(settings, agent_provider=FakeProvider())
    reply = TurnReply(f"Noted: {CANARY}")
    loop = {
        "success": step_and_tool_loop(reply),
        "refusal": canned_loop(reply),  # refused before the loop
        "forced": canned_loop(RuntimeError(FORCED)),
    }[kind]
    # The shop's executor over a scripted connection (one result: the audit row's insert), so the
    # tool call is refused, audited and returns, and the turn replies.
    shop = build_profile(ScriptedSource([[]]), RETAIL, BusinessType.RETAIL)
    service = AgentService(
        {**ALL, shop.business_id: shop},
        gateway=gateway_for(BlockCheckingProvider([reply_json(f"Noted: {CANARY}")])),
        store=RecordingStore(),
        answers=CannedAnswers(),
        limits=LoopLimits(max_steps=4),
        turn_timeout_s=35.0,
        loop=loop,
    )
    app.dependency_overrides[get_agent_service] = lambda: service
    customer = f"Not A Ref {MARK}" if kind == "refusal" else "cust-001"

    def send(client: TestClient) -> httpx2.Response:
        return client.post(
            "/v1/agent",
            json={"message": CANARY, "customer_ref": customer},
            params={"note": CANARY},
            headers={BUSINESS_HEADER: "corner-shop"},
        )

    # The forced exception is the service's own `agent_error` hand-off, a 200 (never a 5xx).
    return Exchange(app, send, 422 if kind == "refusal" else 200)


def confirm(kind: Kind, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Exchange:
    app = create_app(settings)
    # The success path on the route's stand-in; the refusal (an unknown business: no tools) and
    # the bug on the real service over a scripted connection, so its own `tool_call` record runs.
    service: object = {
        "success": StandIn(EXECUTED),
        "refusal": ConfirmService(ScriptedSource(), {}),
        "forced": ConfirmService(ScriptedSource([RuntimeError(FORCED)]), {"paws-and-co": PAWS}),
    }[kind]

    def started(app: FastAPI) -> None:
        app.state.confirm_service = service

    def send(client: TestClient) -> httpx2.Response:
        return client.post(
            CONFIRM_PATH,
            json={"customer_ref": "cust-001", "confirm_token": "Q7zx9TOKENtokenTOKEN_-"},
            params={"note": CANARY},
            headers={BUSINESS_HEADER: "paws-and-co"},
        )

    return Exchange(app, send, {"success": 200, "refusal": 404, "forced": 500}[kind], started)


EXTRACTION_PDF: Final = pdf(
    [line.replace("Asha Rao", f"Asha {MARK}") for line in REFERRAL_LINES] + [f"Note: {CANARY}"]
)


def extractions(kind: Kind, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Exchange:
    answer = with_changes(
        REFERRAL_ANSWER, patient_name=(f"Asha {MARK}", f"Patient name: Asha {MARK}")
    )
    provider = FakeProvider([reply(referral(), answer)])
    store = ExtractionStore(error=RuntimeError(FORCED) if kind == "forced" else None)
    app = create_app(settings)

    def started(app: FastAPI) -> None:
        app.state.extraction_service = ExtractionService(
            FieldExtractor(gateway_on(provider)), route=accept_everything
        )
        app.state.extraction_store = store

    def send(client: TestClient) -> httpx2.Response:
        content_type = "text/plain" if kind == "refusal" else "application/pdf"
        return client.post(
            "/v1/extractions",
            content=EXTRACTION_PDF,
            params={"kind": "referral_letter"},
            headers={BUSINESS_HEADER: SUNRISE_ID, "Content-Type": content_type},
        )

    status = {"success": 201, "refusal": 415, "forced": 500}[kind]
    return Exchange(app, send, status, started)


ROUTES: Final[dict[tuple[str, str], Builder]] = {
    ("GET", "/healthz"): healthz,
    ("GET", "/readyz"): readyz,
    ("GET", "/openapi.json"): docs_page("/openapi.json", "openapi"),
    ("GET", "/docs"): docs_page("/docs", "get_swagger_ui_html"),
    ("GET", "/docs/oauth2-redirect"): docs_page(
        "/docs/oauth2-redirect", "get_swagger_ui_oauth2_redirect_html"
    ),
    ("GET", "/redoc"): docs_page("/redoc", "get_redoc_html"),
    ("POST", "/v1/kb/documents"): kb_documents,
    ("POST", "/v1/kb/reindex"): kb_reindex,
    ("GET", "/v1/kb/search"): kb_search,
    ("POST", "/v1/answer"): answer,
    ("POST", "/v1/agent"): agent,
    ("POST", "/v1/agent/actions/{action_id}/confirm"): confirm,
    ("POST", "/v1/extractions"): extractions,
}
ESCAPES: Final = frozenset(
    {
        ("GET", "/healthz"),
        ("GET", "/openapi.json"),
        ("GET", "/docs"),
        ("GET", "/docs/oauth2-redirect"),
        ("GET", "/redoc"),
        ("POST", "/v1/kb/documents"),
        ("POST", "/v1/kb/reindex"),
        ("GET", "/v1/kb/search"),
        ("POST", "/v1/answer"),
        ("POST", "/v1/agent/actions/{action_id}/confirm"),
        ("POST", "/v1/extractions"),
    }
)
"""The routes whose forced exception escapes the app (a 500, written by the server's error line):
11 of them, so G10-S1 has 11 cases. `/readyz` turns it into a failed check and `/v1/agent` into
its `agent_error` hand-off."""

PATH_CASES: Final[dict[str, tuple[str, int]]] = {
    "confirm action id": (f"/v1/agent/actions/{CANARY}/confirm", 422),
    "unknown route": (f"/v1/{CANARY}", 404),
}
"""G10-S2: customer text in the URL path, and the status each request gets: a malformed
`{action_id}` is a 422 (`uuid_parsing`, naming the location only), an unknown route a 404."""


def cases() -> list[Any]:
    return [
        pytest.param(route, kind, id=f"{route[0]} {route[1]} {kind}")
        for route in ROUTES
        for kind in KINDS
    ]


# ── The tests ────────────────────────────────────────────────────────────────────────────────


def test_every_route_of_the_app_has_its_canary_paths(settings: Settings) -> None:
    app = create_app(settings)
    schema = app.openapi()["paths"]
    routes = {(method.upper(), path) for path, item in schema.items() for method in item}
    routes |= {("GET", r.path) for r in app.routes if isinstance(r, Route)}  # docs, openapi
    # The routers `create_app` includes, read directly: a route with `include_in_schema=False`
    # is missing from the schema, and FastAPI wraps included routers so `app.routes` hides it.
    routers = [router for router in vars(main).values() if isinstance(router, APIRouter)]
    assert len(routers) == 7  # health, kb, search, answer, actions, agent, extraction
    routes |= {
        (method, route.path)
        for router in routers
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }

    assert routes == ROUTES.keys()
    assert ROUTES.keys() >= ESCAPES and len(ESCAPES) == 11
    # The startup self-test ran on the answer-key stand-in (the autouse fixture), not on H1.
    assert app.state.guard_selftest.status is SelftestStatus.PASSED


@pytest.mark.parametrize(("route", "kind"), cases())
def test_no_log_line_holds_the_canary(
    route: tuple[str, str], kind: Kind, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    exchange = ROUTES[route](kind, settings, monkeypatch)

    logged = run(exchange)

    assert logged.response.status_code == exchange.status, logged.response.text
    assert logged.lines, "no line at all: the capture is broken"  # the access line, at least
    assert [(line, parts_in(line)) for line in logged.lines if parts_in(line)] == []
    assert bool(logged.server_errors) == (kind == "forced" and route in ESCAPES)


@pytest.mark.parametrize("route", sorted(ESCAPES), ids=" ".join)
def test_the_servers_error_line_holds_no_canary(
    route: tuple[str, str], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged = run(ROUTES[route]("forced", settings, monkeypatch))

    [line] = logged.server_errors
    exception = json.loads(line)["exception"]
    # The positive control: the forced exception's line, with its class name and its frames.
    assert ASGI_ERROR in json.loads(line)["message"]
    assert exception.startswith("Traceback (most recent call last):\n  File ")
    assert exception.endswith("\nRuntimeError")
    assert parts_in(line) == []
    assert "forced failure" not in line  # nothing of the message, canary or not


def masked_everything(text: str) -> str:
    """A stand-in masker, NOT H2: any text that reaches it becomes `[MASKED]`."""
    return "[MASKED]"


@pytest.mark.parametrize(("route", "kind"), cases())
def test_every_line_but_the_servers_own_is_todays_line(
    route: tuple[str, str], kind: Kind, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With H2 written (2026-11-20) the app's formatter masks free text. A line whose free text
    # holds a G6 shape is no longer today's line (the positive control below); the lines these
    # routes write of their own hold none, so each is still today's, byte for byte, and only the
    # server's two lines change. Whatever a masker would do, a metadata record never reaches it
    # (M3's window: `llm_call` and the other records stay byte-identical).
    logged = run(ROUTES[route](kind, settings, monkeypatch))
    [handler] = logging.getLogger().handlers
    app_formatter = handler.formatter
    assert isinstance(app_formatter, RedactingJsonFormatter) and app_formatter.masker is not None
    assert app_formatter.text(f"call {PHONE}") == "call [PHONE]"
    hostile = RedactingJsonFormatter(masked_everything)

    ours = [r for r in logged.records if not r.name.startswith("uvicorn.")]

    assert ours  # startup and shutdown, at least
    for record in ours:
        assert app_formatter.format(record) == JsonFormatter().format(record)
        if record.getMessage() in METADATA_RECORDS:
            assert hostile.format(record) == JsonFormatter().format(record)


def path_exchange(path: str, status: int, settings: Settings) -> Exchange:
    def send(client: TestClient) -> httpx2.Response:
        return client.post(
            path,
            json={"customer_ref": "cust-001", "confirm_token": "Q7zx9TOKENtokenTOKEN_-"},
            params={"note": CANARY},
            headers={BUSINESS_HEADER: "paws-and-co"},
        )

    return Exchange(create_app(settings), send, status)


PATH_TEMPLATES: Final = {
    "confirm action id": "/v1/agent/actions/{action_id}/confirm",
    "unknown route": UNMATCHED,
}


@pytest.mark.parametrize(
    ("path", "status", "template"),
    [(*PATH_CASES[name], PATH_TEMPLATES[name]) for name in PATH_CASES],
    ids=PATH_CASES.keys(),
)
def test_with_the_canary_in_the_path_no_line_holds_it(
    path: str, status: int, template: str, settings: Settings
) -> None:
    logged = run(path_exchange(path, status, settings))

    assert logged.response.status_code == status, logged.response.text
    [access] = logged.access
    assert json.loads(access)["message"] == (
        f'127.0.0.1:50000 - "POST {template} HTTP/1.1" {status}'
    )
    assert [(line, parts_in(line)) for line in logged.lines if parts_in(line)] == []
    assert logged.server_errors == []


def test_the_agent_success_path_writes_its_model_and_tool_records(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What the success case above covers: the step model's `llm_call` and the read tool's
    # `tool_call` (its canary argument refused as `invalid_arguments`), and a reply.
    logged = run(agent("success", settings, monkeypatch))

    assert logged.response.json()["kind"] == "reply"
    messages = [json.loads(line)["message"] for line in logged.lines]
    assert {"llm_call", "tool_call", "agent_turn"} <= set(messages)
    [tool_call] = [json.loads(line)["tool_call"] for line in logged.lines if '"tool_call"' in line]
    assert (tool_call["outcome"], tool_call["audited"]) == ("invalid_arguments", True)


def test_the_detector_finds_a_planted_line_and_the_access_line_has_no_query(
    settings: Settings,
) -> None:
    # Positive control: a record carrying the canary in its message, in an extra and in its
    # exception, written inside a request, is found in every part.
    app = create_app(settings)

    async def planted() -> CheckResult:
        try:
            raise RuntimeError(FORCED)
        except RuntimeError:
            logging.getLogger("vectapilot_api").warning(
                "planted %s", CANARY, extra={"note": CANARY}, exc_info=True
            )
        return CheckResult("planted", ok=True)

    app.dependency_overrides[health.get_readiness_checks] = lambda: {"planted": planted}

    logged = run(Exchange(app, get("/readyz"), 200))

    # The detector, on the record as plain JSON (`JsonFormatter`, no redaction): every part.
    [record] = [r for r in logged.records if r.getMessage().startswith("planted ")]
    plain = JsonFormatter().format(record)
    assert {MARK, PHONE.removeprefix("+91 "), EMAIL} <= set(parts_in(plain))
    assert plain.count(MARK) == 3  # message, extra and exception
    # The app's line keeps the free text that has no PII shape (masking is not a free-text
    # filter): the mark stays in the message and the extra, with H2 or without it. The exception
    # is class names and frames only, for every logger (decided 2026-11-18, shesha).
    [found] = [line for line in logged.lines if '"planted ' in line]
    assert found.count(MARK) == 2
    exception = json.loads(found)["exception"]
    assert MARK not in exception and exception.endswith("RuntimeError")
    [access] = [line for line in logged.lines if "/readyz" in line]
    assert "?" not in access and "note" not in access  # the access line has no query

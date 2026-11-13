"""M6 PR 3: `POST /v1/extractions`, without a database (ADR 0017 decision 6; E3, E8, E9, E11, E12).

The registry lookup is replaced (as tests/test_business_header.py does) and the store is a
recorder (`served.RecordingStore`), so each rule of the route is pinned on its own; the real store
is in test_store_db.py. Routing is the stand-in `accept_everything` (deliberately not H1) where a
test needs values back; the one test that runs H1 through the route waits for its switch.

- **Order of checks:** the business (400; production refused before anything is read) → the query
  (422, echoing nothing) → the kind (one 404 for unknown, disabled and unconfigured, E8) → the
  format (415) → the size (413 while streaming) → the service's refusals (413, 415, 422, with
  `X-Trace-Id`, nothing stored or sent) → store → 201.
- **201** returns the stored id and expiry and every field, never other document text.
- **Storage failures** are typed 503s with only the exception's type logged (E9); the request limit
  is its own 503, on time even while a PDF is still being read; a bug is a 500.
- **A hostile value never costs the upload** (E3): one NFKC makes longer than its field is withheld
  in review, control characters are dropped, and the outcome always fits migration 0007.
- **No quote, no value** (E2, decided 2026-11-13): a value E2 did not verify comes back in review
  with its reasons and without value, level, page or span.
- **E11:** the route's model is a `FakeProvider` whatever Settings say.
"""

import hashlib
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from tests.canary.read_paths import pending_hand_write
from tests.conftest import make_settings
from tests.extraction.fakes import CANARY, SUNRISE_ID, referral
from tests.extraction.helpers import CORPUS_DIR, EXTRACTION_DIR, MARKER, read_json, write_json
from tests.extraction.served import (
    CREATED,
    STORED_ID,
    RecordingStore,
    extraction_folder,
    served,
)
from tests.extraction.standins import (
    REFERRAL_ANSWER,
    REFERRAL_LINES,
    accept_everything,
    pdf,
    reply,
    with_changes,
)
from tests.extraction.test_text import encrypted_pdf
from tests.test_business_header import registry_says
from vectapilot_api import main
from vectapilot_api.business import REFUSED_ENVIRONMENT
from vectapilot_api.config import Settings
from vectapilot_api.extraction import router as extraction_router
from vectapilot_api.extraction import service as extraction_service
from vectapilot_api.extraction.extractor import build_extraction_gateway
from vectapilot_api.extraction.files import BUSINESSES
from vectapilot_api.extraction.router import NOT_STORED, REFUSALS
from vectapilot_api.extraction.service import storable
from vectapilot_api.extraction.text import (
    OVER_LIMIT,
    DocumentText,
    OcrEngine,
    RefusalReason,
    TextLimits,
    read_pages,
)
from vectapilot_api.gateway.gateway import LLMGateway
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply
from vectapilot_api.kb import errors as kb_errors
from vectapilot_api.log import JsonFormatter

CONFIGURED: Final = make_settings(extraction_config_dir=EXTRACTION_DIR, corpus_dir=CORPUS_DIR)
REFERRAL_PDF: Final = pdf(list(REFERRAL_LINES))
"""The made-up referral; its last line (no field) holds the canary, which must never come back."""
FILLER: Final = "Filler line " + "x" * 88
TRACE_PATTERN: Final = r"^[0-9a-f]{32}$"
FORMATTER: Final = JsonFormatter()


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> None:
    registry_says(monkeypatch, exists=True)


def right_answer() -> FakeReply:
    return reply(referral(), dict(REFERRAL_ANSWER))


def post(
    data: bytes = REFERRAL_PDF,
    *,
    settings: Settings = CONFIGURED,
    store: RecordingStore | None = None,
    script: tuple[FakeReply, ...] | None = None,
    caplog: pytest.LogCaptureFixture | None = None,
    **upload: Any,
) -> tuple[Any, RecordingStore, FakeProvider]:
    """One upload as Sunrise through the app with `accept_everything` and a recording store."""
    recorder = store or RecordingStore()
    provider = FakeProvider(list(script if script is not None else (right_answer(),)))
    with served(settings, provider, route=accept_everything, store=recorder, caplog=caplog) as app:
        response = app.upload(upload.pop("business_id", SUNRISE_ID), data, **upload)
    return response, recorder, provider


def log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(FORMATTER.format(record) for record in caplog.records)


# ── The order of checks ──────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("registered")
def test_production_refuses_the_route_before_anything_is_read() -> None:
    settings = make_settings(
        environment="production",
        llm_provider="anthropic",
        anthropic_api_key="not-a-real-key",
        api_allow_paid_llm=True,
        embeddings_provider="voyage",
        voyage_api_key="not-a-real-key",
        extraction_config_dir=EXTRACTION_DIR,
        corpus_dir=CORPUS_DIR,
    )
    response, store, provider = post(settings=settings)

    assert (response.status_code, response.json()) == (400, {"detail": REFUSED_ENVIRONMENT})
    assert (store.saved, provider.calls) == ([], [])


def test_without_a_business_the_route_is_400_before_the_query_is_read() -> None:
    response, store, provider = post(business_id=None, params={"kind": "Not A Kind!"})

    assert response.status_code == 400 and (store.saved, provider.calls) == ([], [])


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize(
    "params",
    [
        pytest.param({}, id="no kind"),
        pytest.param({"kind": f"{MARKER} kind"}, id="not snake case"),
        pytest.param({"kind": "k" * 41}, id="too long"),
        pytest.param({"kind": "referral_letter", "value": MARKER}, id="an unknown parameter"),
    ],
)
def test_the_query_is_closed_and_a_422_echoes_nothing(params: dict[str, str]) -> None:
    response, store, provider = post(params=params)

    assert response.status_code == 422 and response.json()["error"] == "invalid_request"
    assert MARKER not in response.text and "X-Trace-Id" not in response.headers
    assert (store.saved, provider.calls) == ([], [])


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize(
    ("kind", "settings"),
    [
        pytest.param("invoice", CONFIGURED, id="unknown"),
        pytest.param("vaccination_record", CONFIGURED, id="another business's kind"),
        pytest.param("referral_letter", make_settings(), id="no extraction folder"),
    ],
)
def test_an_unknown_a_disabled_and_an_unconfigured_kind_are_the_same_404(
    kind: str, settings: Settings
) -> None:
    response, store, provider = post(kind=kind, settings=settings, content_type="text/plain")

    assert (response.status_code, response.json()) == (
        404,
        {"detail": "this business has no extraction of this kind", "error": "kind_not_found"},
    )
    assert "X-Trace-Id" not in response.headers and (store.saved, provider.calls) == ([], [])


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize("content_type", [None, "text/plain", "image/png", "application/pdfx"])
def test_a_body_not_declared_as_pdf_is_415_before_it_is_read(content_type: str | None) -> None:
    response, store, provider = post(content_type=content_type)

    assert (response.status_code, response.json()["error"]) == (415, "unsupported_media_type")
    assert "X-Trace-Id" not in response.headers and (store.saved, provider.calls) == ([], [])


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize(
    "content_type", ["Application/PDF; charset=binary", "application/pdf ; charset=binary"]
)
def test_content_type_parameters_case_and_whitespace_do_not_matter(content_type: str) -> None:
    response, store, _ = post(content_type=content_type)

    assert response.status_code == 201 and len(store.saved) == 1


@pytest.mark.usefixtures("registered")
def test_a_body_over_the_byte_limit_is_413_before_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extraction_router, "MAX_BYTES", len(REFERRAL_PDF) - 1)
    response, store, provider = post()

    assert (response.status_code, response.json()["error"]) == (413, "file_too_large")
    assert "X-Trace-Id" not in response.headers and (store.saved, provider.calls) == ([], [])


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize(
    ("kind", "content_type", "status"),
    [
        pytest.param("invoice", extraction_router.PDF, 404, id="kind"),
        pytest.param("referral_letter", "text/plain", 415, id="format"),
    ],
)
def test_the_kind_and_the_format_are_checked_before_the_size(
    monkeypatch: pytest.MonkeyPatch, kind: str, content_type: str, status: int
) -> None:
    # The body is over the limit too: reading it first would make each of these a 413.
    monkeypatch.setattr(extraction_router, "MAX_BYTES", len(REFERRAL_PDF) - 1)
    response, store, provider = post(kind=kind, content_type=content_type)

    assert response.status_code == status
    assert (store.saved, provider.calls) == ([], [])


# ── Refused by the service (E12 and unreadable bodies): typed, traced, nothing stored or sent ──


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize(
    ("data", "status", "code"),
    [
        pytest.param(pdf(*[list(REFERRAL_LINES)] * 6), 413, "too_many_pages", id="6 pages"),
        pytest.param(pdf([FILLER] * 210), 413, "text_too_long", id="21000 characters"),
        pytest.param(f"Patient name: {CANARY}".encode(), 415, "not_pdf", id="not a pdf"),
        pytest.param(encrypted_pdf(), 422, "encrypted_document", id="encrypted"),
        pytest.param(f"%PDF-1.4 {CANARY}".encode(), 422, "unreadable_document", id="unreadable"),
    ],
)
def test_a_refused_document_is_typed_traced_and_never_stored_or_sent(
    data: bytes, status: int, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    response, store, provider = post(data, script=(), caplog=caplog)

    assert (response.status_code, response.json()["error"]) == (status, code)
    assert set(response.json()) == {"detail", "error"}
    assert response.headers["X-Trace-Id"] in log_text(caplog)  # the `extraction` record's
    assert (store.saved, provider.calls) == ([], [])
    assert CANARY not in response.text and CANARY not in log_text(caplog)
    assert NOT_STORED not in log_text(caplog)  # refused, not a storage failure


def test_every_refusal_has_a_typed_error_and_only_the_limits_are_413() -> None:
    assert set(REFUSALS) == set(RefusalReason)
    assert {reason for reason, (status, _, _) in REFUSALS.items() if status == 413} == OVER_LIMIT
    assert len({code for _, code, _ in REFUSALS.values()}) == len(REFUSALS)


# ── 201 ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("registered")
def test_a_stored_extraction_is_201_with_every_field_and_no_other_document_text() -> None:
    response, store, _ = post()

    assert response.status_code == 201, response.text
    [saved] = store.saved
    body = response.json()
    assert response.headers["X-Trace-Id"] == saved.trace_id
    assert saved.file_sha256 == hashlib.sha256(REFERRAL_PDF).hexdigest()
    assert saved.retention_days == referral().retention_days
    assert saved.outcome.business_id == saved.business_id == SUNRISE_ID  # the header's business
    assert body["id"] == str(STORED_ID) and body["kind"] == "referral_letter"
    assert (body["status"], body["failure"], body["pages"], body["image_pages"]) == (
        "needs_review",  # the policy number is always reviewed (identifiers, decided 2026-11-12)
        None,
        1,
        [],
    )
    expires = datetime.fromisoformat(body["expires_at"])
    assert expires == CREATED + timedelta(days=referral().retention_days)
    assert [f["field"] for f in body["fields"]] == [s.name for s in referral().template.fields]
    by_name = {f["field"]: f for f in body["fields"]}
    assert by_name["patient_name"] == {
        "field": "patient_name",
        "status": "accepted",
        "level": "high",
        "reasons": [],
        "value": "Asha Rao",
        "span": "Patient name: Asha Rao",
        "page": 1,
    }
    # `accept_everything` accepted the always-review identifier; the invariant check moved it.
    assert (by_name["policy_number"]["status"], by_name["policy_number"]["reasons"]) == (
        "review",
        ["routing_invariant"],
    )
    assert CANARY not in response.text  # the document's other lines never come back


@pytest.mark.usefixtures("registered")
def test_the_businesss_own_retention_reaches_the_store_and_the_expiry(tmp_path: Path) -> None:
    folder, corpus = extraction_folder(tmp_path, {SUNRISE_ID: "sunrise-clinic"})
    business_file = folder / BUSINESSES / f"{SUNRISE_ID}.json"
    write_json(business_file, read_json(business_file) | {"retention_days": 7})  # not the default
    settings = make_settings(extraction_config_dir=folder, corpus_dir=corpus)
    response, store, _ = post(settings=settings)

    assert response.status_code == 201, response.text
    assert store.saved[0].retention_days == 7
    assert datetime.fromisoformat(response.json()["expires_at"]) == CREATED + timedelta(days=7)


# ── A hostile value never costs the upload (E3; M6 PR 3 review) ─────────────────────────────


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize(
    ("proposed", "expected"),
    [
        pytest.param(
            ("\ufdfa" * 150, "nowhere in the document"),
            ("review", None, None, None),
            id="150 characters NFKC makes 2700",
        ),
        pytest.param(
            ("Asha\x00 Rao", "Patient name:\x00 Asha Rao"),
            ("accepted", "Asha Rao", "Patient name: Asha Rao", 1),
            id="a NUL Postgres refuses",
        ),
    ],
)
def test_a_hostile_value_is_stored_201_and_always_fits_the_columns(
    proposed: tuple[str, str], expected: tuple[object, ...]
) -> None:
    answer = with_changes(REFERRAL_ANSWER, patient_name=proposed)
    response, store, _ = post(script=(reply(referral(), answer),))

    assert response.status_code == 201, response.text
    [saved] = store.saved
    assert storable(saved.outcome)  # what PostgresExtractionStore would store, never a 503
    field = {f["field"]: f for f in response.json()["fields"]}["patient_name"]
    assert (field["status"], field["value"], field["span"], field["page"]) == expected
    assert response.json()["failure"] is None
    assert ("too_long" in field["reasons"]) == (expected[1] is None)
    assert "\x00" not in response.text and "\\u0000" not in response.text


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize(
    "proposed",
    [
        ("Priya Shah", "Patient name: Priya Shah"),
        ("Priya Rao", "Patient name: Asha Rao"),
        ("Asha", "Patient name: Asha"),
    ],
    ids=["an invented quote", "a real quote with another value", "a value cut short"],
)
def test_a_value_e2_did_not_verify_is_returned_without_value_level_page_or_span(
    proposed: tuple[str, str],
) -> None:
    # Decided 2026-11-13 (shesha, M6 PR 3 review): no quote, no value, in the response too.
    answer = with_changes(REFERRAL_ANSWER, patient_name=proposed)
    response, store, _ = post(script=(reply(referral(), answer),))

    assert response.status_code == 201, response.text
    field = {f["field"]: f for f in response.json()["fields"]}["patient_name"]
    assert field == {
        "field": "patient_name",
        "status": "review",
        "level": None,
        "reasons": ["routing_invariant"],
        "value": None,
        "span": None,
        "page": None,
    }
    [saved] = store.saved
    assert saved.outcome.unverified == ("patient_name",)
    assert proposed[0] not in response.text  # the guess never comes back, anywhere


# ── Failures after the service ────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize(
    "error",
    [
        OperationalError("INSERT", {}, Exception(CANARY)),
        OSError(CANARY),
        TimeoutError(CANARY),  # the database's own timeout, not the request limit
    ],
    ids=["sqlalchemy", "oserror", "inner_timeout"],
)
def test_a_storage_failure_is_a_typed_503_with_only_its_type_logged(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    lines = [line.replace("Asha Rao", f"Asha {CANARY}") for line in REFERRAL_LINES]
    answer = with_changes(
        REFERRAL_ANSWER, patient_name=(f"Asha {CANARY}", f"Patient name: Asha {CANARY}")
    )
    response, store, _ = post(
        pdf(lines),
        store=RecordingStore(error=error),
        script=(reply(referral(), answer),),
        caplog=caplog,
    )

    assert (response.status_code, response.json()) == (
        503,
        {"detail": "the database is unavailable; try again later", "error": "database_unavailable"},
    )
    [saved] = store.saved
    assert CANARY in (saved.outcome.field_of("patient_name").value or "")  # the positive control
    [record] = [r for r in caplog.records if r.getMessage() == NOT_STORED]
    assert getattr(record, NOT_STORED) == {
        "business_id": SUNRISE_ID,
        "trace_id": response.headers["X-Trace-Id"],
        "kind": "referral_letter",
        "code": "database_unavailable",
        "error": type(error).__name__,
    }
    assert CANARY not in log_text(caplog) and CANARY not in response.text
    assert not any(r.exc_info for r in caplog.records)


@pytest.mark.usefixtures("registered")
def test_the_request_limit_is_its_own_503(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Long enough for the extraction to reach the store, far below its `HANG_S`.
    monkeypatch.setattr(extraction_router, "REQUEST_TIMEOUT_S", 0.5)
    response, store, _ = post(store=RecordingStore(hang=True), caplog=caplog)

    assert (response.status_code, response.json()["error"]) == (503, "extraction_timeout")
    assert response.headers["X-Trace-Id"] == store.saved[0].trace_id
    [record] = [r for r in caplog.records if r.getMessage() == NOT_STORED]
    assert getattr(record, NOT_STORED)["error"] == "TimeoutError"


def unreadable() -> dict[str, Any]:
    return {"data": f"%PDF-1.4 {CANARY}".encode(), "script": ()}


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize(
    ("upload", "limit_s", "status", "code"),
    [
        pytest.param(
            lambda: {
                "store": RecordingStore(error=OperationalError("INSERT", {}, Exception(CANARY)))
            },
            None,
            503,
            "database_unavailable",
            id="sqlalchemy",
        ),
        pytest.param(
            lambda: {"store": RecordingStore(error=OSError(CANARY))},
            None,
            503,
            "database_unavailable",
            id="oserror",
        ),
        pytest.param(
            lambda: {"store": RecordingStore(error=TimeoutError(CANARY))},
            None,
            503,
            "database_unavailable",
            id="inner_timeout",
        ),
        pytest.param(
            lambda: {"store": RecordingStore(hang=True)},
            0.05,
            503,
            "extraction_timeout",
            id="request_limit",
        ),
        pytest.param(unreadable, None, 422, "unreadable_document", id="refused"),
    ],
)
def test_a_typed_error_after_the_service_is_raised_without_a_chained_cause(
    monkeypatch: pytest.MonkeyPatch,
    upload: Callable[[], dict[str, Any]],
    limit_s: float | None,
    status: int,
    code: str,
) -> None:
    # Raised inside `except`, the 503 or refusal would carry the caught error (and the database
    # text it may hold) as `__context__`, which a traceback or an error tracker prints (E9).
    seen: list[tuple[BaseException | None, BaseException | None]] = []
    real = kb_errors.kb_error_handler

    async def spy(request: Request, exc: Exception) -> JSONResponse:
        seen.append((exc.__context__, exc.__cause__))
        return await real(request, exc)

    monkeypatch.setattr(kb_errors, "kb_error_handler", spy)  # before `create_app` installs it
    if limit_s is not None:
        monkeypatch.setattr(extraction_router, "REQUEST_TIMEOUT_S", limit_s)
    response, _, _ = post(**upload())

    assert (response.status_code, response.json()["error"]) == (status, code)
    assert seen == [(None, None)]


class BlockedRead:
    """`read_pages` held in its worker thread until `release` is set (at most 2 s)."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def __call__(self, data: bytes, *, limits: TextLimits, ocr: OcrEngine) -> DocumentText:
        self.release.wait(timeout=2)
        return read_pages(data, limits=limits, ocr=ocr)


@pytest.mark.usefixtures("registered")
def test_the_request_limit_answers_while_a_pdf_is_still_being_read(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    read = BlockedRead()
    monkeypatch.setattr(extraction_service, "read_pages", read)
    monkeypatch.setattr(extraction_router, "REQUEST_TIMEOUT_S", 0.05)
    store, provider = RecordingStore(), FakeProvider([right_answer()])
    with served(CONFIGURED, provider, route=accept_everything, store=store, caplog=caplog) as app:
        started = time.monotonic()
        try:
            response = app.upload(SUNRISE_ID, REFERRAL_PDF)
            elapsed = time.monotonic() - started
        finally:
            read.release.set()  # the read's thread finishes on its own (a hard kill is M9's)

    assert (response.status_code, response.json()["error"]) == (503, "extraction_timeout")
    assert elapsed < 1.0  # pypdf on the event loop would have held the request for 2 s
    assert (store.saved, provider.calls) == ([], [])
    [record] = [r for r in caplog.records if r.getMessage() == NOT_STORED]
    assert getattr(record, NOT_STORED)["trace_id"] == response.headers["X-Trace-Id"]


@pytest.mark.usefixtures("registered")
def test_a_bug_is_a_500_never_a_typed_error() -> None:
    response, _, _ = post(store=RecordingStore(error=KeyError(CANARY)))

    assert response.status_code == 500 and "X-Trace-Id" not in response.headers
    assert CANARY not in response.text


# ── E11: the fake provider, whatever the settings ─────────────────────────────────────────────


@pytest.mark.parametrize("llm", [None, FakeProvider([])], ids=["settings", "injected"])
def test_the_routes_model_is_the_fake_provider_whatever_the_settings(
    monkeypatch: pytest.MonkeyPatch, llm: FakeProvider | None
) -> None:
    given: list[Provider] = []
    real = build_extraction_gateway

    @asynccontextmanager
    async def recording(settings: Settings, *, provider: Provider) -> AsyncIterator[LLMGateway]:
        given.append(provider)
        async with real(settings, provider=provider) as gateway:
            yield gateway

    monkeypatch.setattr("vectapilot_api.main.build_extraction_gateway", recording)
    paid = make_settings(
        environment="local",
        llm_provider="anthropic",
        anthropic_api_key="not-a-real-key",
        api_allow_paid_llm=True,
        extraction_config_dir=EXTRACTION_DIR,
        corpus_dir=CORPUS_DIR,
    )
    with TestClient(main.create_app(paid, llm_provider=llm)):
        pass

    # Its own fake: never the paid provider Settings name, nor the answer path's injected one.
    assert len(given) == 1 and isinstance(given[0], FakeProvider) and given[0] is not llm


def test_only_a_fake_provider_may_be_given_to_the_route() -> None:
    class Paid:
        async def send(self, request: object) -> object:
            raise AssertionError("never called")

        async def aclose(self) -> None:
            return None

    with pytest.raises(TypeError, match="FakeProvider"):
        main.create_app(CONFIGURED, extraction_provider=Paid())  # type: ignore[arg-type]


# ── H1 through the route: waits for its switch ────────────────────────────────────────────────


def waits_for_h1[F: Callable[..., Any]](test: F) -> F:
    """The one switch's marks (`PENDING_HAND_WRITE`): a strict xfail while `route_fields` is a
    stub."""
    for mark in pending_hand_write("route_fields"):
        test = mark(test)
    return test


@waits_for_h1
@pytest.mark.usefixtures("registered")
def test_the_route_runs_h1s_routing_with_the_scripted_fake() -> None:
    store = RecordingStore()
    with served(CONFIGURED, FakeProvider([right_answer()]), store=store) as app:
        response = app.upload(SUNRISE_ID, REFERRAL_PDF)

    assert response.status_code == 201, response.text
    [saved] = store.saved
    if saved.outcome.error == "NotImplementedError":  # H1's stub, failed closed into review
        raise NotImplementedError("route_fields (M6 H1) is pending: the route stored all-review")
    statuses = {f["field"]: f["status"] for f in response.json()["fields"]}
    assert statuses == {
        "patient_name": "accepted",
        "date_of_birth": "accepted",
        "referring_doctor": "accepted",
        "referral_date": "accepted",
        "insurer": "accepted",
        "policy_number": "review",
    }

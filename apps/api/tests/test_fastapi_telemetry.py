"""FastAPI's built-in OpenTelemetry stays off (M3 PR 1; ADR 0007 amendment; ADR 0011 decision 9).

FastAPI 0.143.0 records spans, metrics and logs by default as soon as any OpenTelemetry provider is
configured, and reads an inbound `traceparent` (`fastapi/telemetry/_asgi.py`, `NativeTelemetry`).
`create_app` passes `main.TELEMETRY_OFF`. These tests send four requests through the real app (the
search and answer services on stand-ins, no database): a search whose `?q=` holds a canary, a 422
on `POST /v1/answer` whose body holds it, a forced 500 whose exception message holds it, and a
search carrying a `traceparent`. One assertion per signal: no span, no metric, no log record, and
the `traceparent` never read. The red proof sends the same requests with FastAPI's defaults, and
every recorder fills, the canary included. No recorder can see `operation_spans` or
`auto_configure` while the signals are off, so one more test reads what FastAPI kept from
`create_app`'s call, all five switches.

**How FastAPI sees the recorders: as the process-wide providers.** That is FastAPI's default
(`TelemetryConfig`: "Uses global providers by default"), and the risk is a global provider set by
an SDK or `opentelemetry-instrument`, so the test sets up exactly that. Not a `create_app`
parameter: production never passes providers, so it would test a path the server never takes,
and the factory would grow a test-only argument. The fixture swaps opentelemetry-api's private
globals with `monkeypatch`, which puts the originals back after each test. Not the public
`set_*_provider`: each works once per process (`Once`), and `set_meter_provider` also rebinds every
proxy meter for good (`metrics/_internal/__init__.py:856-867`), which no fixture can undo. A
renamed private global makes `monkeypatch.setattr` raise: the test fails loudly, never silently.

**The recorders** are hand-written subclasses of opentelemetry-api's abstract `TracerProvider`,
`MeterProvider` and `_logs.LoggerProvider` (not the NoOp providers: FastAPI treats those as off),
and of `TextMapPropagator`. No `opentelemetry-sdk`. Each provider records the request for a
tracer, meter or logger (nothing can record without one) and then every span, measurement and
log record made through it.
"""

import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.telemetry import get_telemetry_data
from fastapi.testclient import TestClient
from opentelemetry import propagate as otel_propagate
from opentelemetry import trace as otel_trace
from opentelemetry._logs import Logger, LoggerProvider, LogRecord, NoOpLogger, SeverityNumber
from opentelemetry._logs import _internal as otel_logs_internal
from opentelemetry.context import Context
from opentelemetry.metrics import (
    Counter,
    Histogram,
    Meter,
    MeterProvider,
    NoOpCounter,
    NoOpHistogram,
    NoOpMeter,
    NoOpUpDownCounter,
    UpDownCounter,
)
from opentelemetry.metrics import _internal as otel_metrics_internal
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    TextMapPropagator,
    default_getter,
    default_setter,
)
from opentelemetry.trace import (
    INVALID_SPAN_CONTEXT,
    Link,
    NonRecordingSpan,
    NoOpTracer,
    Span,
    SpanKind,
    Tracer,
    TracerProvider,
)
from opentelemetry.util.types import AnyValue, Attributes

from tests.answer.apps import CONFIG, A, gateway_for, headers
from tests.conftest import UNREACHABLE_SETTINGS
from tests.retrieval.standins import build_search, hit
from vectapilot_api import main
from vectapilot_api.answer.router import get_answer_service
from vectapilot_api.answer.service import AnswerService
from vectapilot_api.answer.types import AnswerRun
from vectapilot_api.db import Database
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.retrieval.router import TRACE_HEADER, get_search_service

CANARY: Final = f"canary-otel-{uuid.uuid4().hex[:12]}"
"""In the `?q=`, the 422's body and the 500's message: where FastAPI's telemetry would copy it."""

INBOUND_TRACE_ID: Final = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT: Final = f"00-{INBOUND_TRACE_ID}-00f067aa0ba902b7-01"
"""A client's W3C trace context (the example from the spec)."""


# ── Recorders: opentelemetry-api's abstract providers, hand-written ──────────────────────────


@dataclass
class RecordedSpan:
    name: str
    attributes: dict[str, object]


class RecordingSpan(NonRecordingSpan):
    """A span that keeps its name and attributes (FastAPI sets most of them after the start)."""

    def __init__(self, recorded: RecordedSpan) -> None:
        super().__init__(INVALID_SPAN_CONTEXT)
        self.recorded = recorded

    def set_attributes(self, attributes: Mapping[str, AnyValue]) -> None:
        self.recorded.attributes.update(attributes)

    def set_attribute(self, key: str, value: AnyValue) -> None:
        self.recorded.attributes[key] = value

    def update_name(self, name: str) -> None:
        self.recorded.name = name


class RecordingTracer(NoOpTracer):
    """Every span started (NoOpTracer's `start_as_current_span` calls `start_span`)."""

    def __init__(self, recorded: list[object]) -> None:
        self._recorded = recorded

    def start_span(
        self,
        name: str,
        context: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
    ) -> Span:
        span = RecordedSpan(name, dict(attributes or {}))
        self._recorded.append(span)
        return RecordingSpan(span)


class RecordingTracerProvider(TracerProvider):
    def __init__(self) -> None:
        self.recorded: list[object] = []
        """`("tracer", name)` for each tracer handed out, then every `RecordedSpan`."""

    def get_tracer(
        self,
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
        attributes: Attributes = None,
    ) -> Tracer:
        self.recorded.append(("tracer", instrumenting_module_name))
        return RecordingTracer(self.recorded)


@dataclass(frozen=True)
class Measurement:
    instrument: str
    amount: float
    attributes: dict[str, object]


class RecordingHistogram(NoOpHistogram):
    def __init__(self, name: str, recorded: list[object]) -> None:
        super().__init__(name)
        self._name = name
        self._recorded = recorded

    def record(
        self, amount: float, attributes: Attributes = None, context: Context | None = None
    ) -> None:
        self._recorded.append(Measurement(self._name, amount, dict(attributes or {})))


class RecordingUpDownCounter(NoOpUpDownCounter):
    def __init__(self, name: str, recorded: list[object]) -> None:
        super().__init__(name)
        self._name = name
        self._recorded = recorded

    def add(
        self, amount: float, attributes: Attributes = None, context: Context | None = None
    ) -> None:
        self._recorded.append(Measurement(self._name, amount, dict(attributes or {})))


class RecordingCounter(NoOpCounter):
    def __init__(self, name: str, recorded: list[object]) -> None:
        super().__init__(name)
        self._name = name
        self._recorded = recorded

    def add(
        self, amount: float, attributes: Attributes = None, context: Context | None = None
    ) -> None:
        self._recorded.append(Measurement(self._name, amount, dict(attributes or {})))


class RecordingMeter(NoOpMeter):
    """The synchronous instruments record every measurement; any other instrument still needs
    this meter, and the provider recorded handing it out."""

    def __init__(self, name: str, recorded: list[object]) -> None:
        super().__init__(name)
        self._recorded = recorded

    def create_counter(self, name: str, unit: str = "", description: str = "") -> Counter:
        return RecordingCounter(name, self._recorded)

    def create_up_down_counter(
        self, name: str, unit: str = "", description: str = ""
    ) -> UpDownCounter:
        return RecordingUpDownCounter(name, self._recorded)

    def create_histogram(
        self,
        name: str,
        unit: str = "",
        description: str = "",
        *,
        explicit_bucket_boundaries_advisory: Sequence[float] | None = None,
    ) -> Histogram:
        return RecordingHistogram(name, self._recorded)


class RecordingMeterProvider(MeterProvider):
    def __init__(self) -> None:
        self.recorded: list[object] = []
        """`("meter", name)` for each meter handed out, then every `Measurement`."""

    def get_meter(
        self,
        name: str,
        version: str | None = None,
        schema_url: str | None = None,
        attributes: Attributes = None,
    ) -> Meter:
        self.recorded.append(("meter", name))
        return RecordingMeter(name, self.recorded)


@dataclass(frozen=True)
class EmittedRecord:
    event_name: str | None
    body: object
    exception: str | None
    request_body: object
    """What FastAPI hands any log processor for this request (`get_telemetry_data`): the body of
    a failed validation, as received."""


class RecordingLogger(NoOpLogger):
    def __init__(self, name: str, recorded: list[object]) -> None:
        super().__init__(name)
        self._recorded = recorded

    def emit(
        self,
        record: LogRecord | None = None,
        *,
        timestamp: int | None = None,
        observed_timestamp: int | None = None,
        context: Context | None = None,
        severity_number: SeverityNumber | None = None,
        severity_text: str | None = None,
        body: AnyValue | None = None,
        attributes: Attributes = None,
        event_name: str | None = None,
        exception: BaseException | None = None,
    ) -> None:
        data = get_telemetry_data(context)
        self._recorded.append(
            EmittedRecord(
                event_name=event_name,
                body=body,
                exception=None if exception is None else str(exception),
                request_body=None if data is None else data.body,
            )
        )

    def enabled(
        self,
        *,
        context: Context | None = None,
        severity_number: SeverityNumber | None = None,
        event_name: str | None = None,
    ) -> bool:
        return True


class RecordingLoggerProvider(LoggerProvider):
    def __init__(self) -> None:
        self.recorded: list[object] = []
        """`("logger", name)` for each logger handed out, then every `EmittedRecord`."""

    def get_logger(
        self,
        name: str,
        version: str | None = None,
        schema_url: str | None = None,
        attributes: Attributes = None,
    ) -> Logger:
        self.recorded.append(("logger", name))
        return RecordingLogger(name, self.recorded)


class RecordingPropagator(TextMapPropagator):
    """The global propagator: records what each `extract` read as `traceparent`."""

    def __init__(self) -> None:
        self.extracted: list[list[str] | None] = []

    def extract(
        self,
        carrier: CarrierT,
        context: Context | None = None,
        getter: Getter[CarrierT] = default_getter,
    ) -> Context:
        self.extracted.append(getter.get(carrier, "traceparent"))
        return context if context is not None else Context()

    def inject(
        self,
        carrier: CarrierT,
        context: Context | None = None,
        setter: Setter[CarrierT] = default_setter,
    ) -> None:
        return None

    @property
    def fields(self) -> set[str]:
        return {"traceparent"}


@dataclass
class Recorders:
    traces: RecordingTracerProvider = field(default_factory=RecordingTracerProvider)
    metrics: RecordingMeterProvider = field(default_factory=RecordingMeterProvider)
    logs: RecordingLoggerProvider = field(default_factory=RecordingLoggerProvider)
    propagator: RecordingPropagator = field(default_factory=RecordingPropagator)


def install_recorders(mp: pytest.MonkeyPatch) -> Recorders:
    """New recorders as the process-wide providers and propagator, until `mp` is undone."""
    found = Recorders()
    mp.setattr(otel_trace, "_TRACER_PROVIDER", found.traces)
    mp.setattr(otel_metrics_internal, "_METER_PROVIDER", found.metrics)
    mp.setattr(otel_logs_internal, "_LOGGER_PROVIDER", found.logs)
    mp.setattr(otel_propagate, "_HTTP_TEXT_FORMAT", found.propagator)
    # Through the public getters FastAPI calls (`telemetry/_asgi.py`), they are the globals now:
    assert otel_trace.get_tracer_provider() is found.traces
    assert otel_metrics_internal.get_meter_provider() is found.metrics
    assert otel_logs_internal.get_logger_provider() is found.logs
    assert otel_propagate.get_global_textmap() is found.propagator
    return found


@pytest.fixture
def recorders(monkeypatch: pytest.MonkeyPatch) -> Iterator[Recorders]:
    """The recorders as the process-wide providers and propagator; the originals come back after
    the test (monkeypatch's undo)."""
    yield install_recorders(monkeypatch)


# ── The four requests ────────────────────────────────────────────────────────────────────────


class FailingAnswers(AnswerService):
    """A bug in the answer step: the route re-raises it, so the client gets a 500."""

    async def answer(self, business_id: str, question: str, *, trace_id: str) -> AnswerRun:
        raise RuntimeError(f"forced failure {CANARY}")


@dataclass(frozen=True)
class Exchanges:
    search: httpx2.Response
    invalid: httpx2.Response
    failed: httpx2.Response
    traced: httpx2.Response


def exchange(monkeypatch: pytest.MonkeyPatch) -> Exchanges:
    """The real app (`create_app`), its services on stand-ins (`tests/answer/apps.py`)."""

    async def registered(self: Database, business_id: str) -> bool:
        return True

    monkeypatch.setattr(Database, "business_exists", registered)
    search = build_search({A: [hit("We open at 9 every weekday.", 0.9)]})
    failing = FailingAnswers(search.service, gateway_for(FakeProvider()), CONFIG)
    app: FastAPI = main.create_app(UNREACHABLE_SETTINGS.model_copy())
    app.dependency_overrides[get_search_service] = lambda: search.service
    app.dependency_overrides[get_answer_service] = lambda: failing
    # `raise_server_exceptions=False`: the 500 reaches the client, as from a real server.
    with TestClient(app, raise_server_exceptions=False) as client:
        return Exchanges(
            search=client.get(
                "/v1/kb/search", params={"q": f"opening hours {CANARY}"}, headers=headers(A)
            ),
            invalid=client.post(
                "/v1/answer", json={"question": "hours?", "note": CANARY}, headers=headers(A)
            ),
            failed=client.post("/v1/answer", json={"question": "hours?"}, headers=headers(A)),
            traced=client.get(
                "/v1/kb/search",
                params={"q": "opening hours"},
                headers={**headers(A), "traceparent": TRACEPARENT},
            ),
        )


def assert_statuses(exchanges: Exchanges) -> None:
    assert [
        exchanges.search.status_code,
        exchanges.invalid.status_code,
        exchanges.failed.status_code,
        exchanges.traced.status_code,
    ] == [200, 422, 500, 200]


@pytest.fixture
def pinned_off(monkeypatch: pytest.MonkeyPatch, recorders: Recorders) -> Recorders:
    """The four requests through the app as `create_app` builds it, the recorders configured."""
    assert_statuses(exchange(monkeypatch))
    return recorders


# ── Telemetry off: one assertion per signal ─────────────────────────────────────────────────


def test_fastapi_makes_no_span(pinned_off: Recorders) -> None:
    assert pinned_off.traces.recorded == []


def test_fastapi_records_no_metric(pinned_off: Recorders) -> None:
    assert pinned_off.metrics.recorded == []


def test_fastapi_emits_no_log_record(pinned_off: Recorders) -> None:
    assert pinned_off.logs.recorded == []


def test_fastapi_never_reads_the_inbound_traceparent(pinned_off: Recorders) -> None:
    assert pinned_off.propagator.extracted == []


def test_the_trace_id_stays_server_made(monkeypatch: pytest.MonkeyPatch) -> None:
    # ADR 0011 decision 9: a random id the server made, never the client's trace id.
    traced = exchange(monkeypatch).traced

    assert traced.headers[TRACE_HEADER] == traced.json()["trace_id"]
    assert INBOUND_TRACE_ID not in (traced.headers[TRACE_HEADER], traced.text)


def test_create_app_pins_every_telemetry_switch_off() -> None:
    # `operation_spans` and `auto_configure` change nothing while the three signals are off (no
    # recorder can see them); pinned so that turning a signal on later is a deliberate change.
    assert main.TELEMETRY_OFF == {
        "tracing": False,
        "metrics": False,
        "logs": False,
        "operation_spans": False,
        "auto_configure": False,
    }


def test_the_app_create_app_builds_keeps_every_switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # What the app will use, not only the constant: FastAPI 0.143.0 keeps its defaults merged with
    # the `telemetry` argument in `FastAPI._telemetry` (`applications.py:1033-1045`; a rename
    # fails here loudly). A call that drops or flips a switch is caught, `operation_spans` and
    # `auto_configure` included. Dropped, `auto_configure` falls back to this variable, so it is
    # set: the pin must also win over the environment.
    monkeypatch.setenv("FASTAPI_OTEL_AUTO_CONFIGURE", "true")
    assert FastAPI()._telemetry["auto_configure"] is True  # the premise: the variable is read

    stored = main.create_app(UNREACHABLE_SETTINGS.model_copy())._telemetry

    assert {
        "tracing": stored["tracing"],
        "metrics": stored["metrics"],
        "logs": stored["logs"],
        "operation_spans": stored["operation_spans"],
        "auto_configure": stored["auto_configure"],
    } == main.TELEMETRY_OFF


# ── Red proof: FastAPI's defaults, the same requests ────────────────────────────────────────


def test_red_proof_fastapis_defaults_record_all_of_it(
    monkeypatch: pytest.MonkeyPatch, recorders: Recorders
) -> None:
    # FastAPI's defaults for every signal; auto-configure off so the developer's environment
    # cannot change the test (create_app reads TELEMETRY_OFF).
    monkeypatch.setattr(main, "TELEMETRY_OFF", {"auto_configure": False})
    assert_statuses(exchange(monkeypatch))

    spans = [s for s in recorders.traces.recorded if isinstance(s, RecordedSpan)]
    assert any(CANARY in str(s.attributes.get("url.query")) for s in spans), spans
    measured = {m.instrument for m in recorders.metrics.recorded if isinstance(m, Measurement)}
    assert measured == {"http.server.request.duration", "http.server.active_requests"}
    records = [r for r in recorders.logs.recorded if isinstance(r, EmittedRecord)]
    assert any(
        r.event_name == "fastapi.validation.failed" and CANARY in str(r.request_body)
        for r in records
    ), records
    assert any(
        r.event_name == "http.server.request.exception" and CANARY in str(r.exception)
        for r in records
    ), records
    assert [TRACEPARENT] in recorders.propagator.extracted


def test_installing_the_recorders_leaves_the_globals_as_it_found_them() -> None:
    originals = (
        otel_trace._TRACER_PROVIDER,
        otel_metrics_internal._METER_PROVIDER,
        otel_logs_internal._LOGGER_PROVIDER,
        otel_propagate._HTTP_TEXT_FORMAT,
    )

    with pytest.MonkeyPatch.context() as mp:
        found = install_recorders(mp)
        assert otel_trace.get_tracer_provider() is found.traces
        assert otel_metrics_internal.get_meter_provider() is found.metrics
        assert otel_logs_internal.get_logger_provider() is found.logs
        assert otel_propagate.get_global_textmap() is found.propagator

    assert otel_trace._TRACER_PROVIDER is originals[0]
    assert otel_metrics_internal._METER_PROVIDER is originals[1]
    assert otel_logs_internal._LOGGER_PROVIDER is originals[2]
    assert otel_propagate._HTTP_TEXT_FORMAT is originals[3]

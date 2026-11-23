"""M7 PR 7: the document path's runner (`redteam/document.py`) and its PDF (`redteam/pdf.py`). Unit.

Category D goes through the route's real guard object (`ExtractionGuard`: `admit`, `scan`,
`finish`) with the in-process world's guard folder. With H1's stub the screen fails closed, so
every case is `pending:H1`. With `tests/redteam/standins.py`'s `CategoryLookup` (a fixed lookup
from a whole page's text to a category, NOT H1) and every probe saying "written", d-001 to d-004
pass for the reason the scorer demands (`allowed` decided by `none`, with D's findings, the
hidden-text signal included), and d-005 stays a miss: the screen flags a page, it never hands an
upload off (the forced review is PR 11's, so d-005 is a known gap). The database environment's
plumbing runs here on a fake served suite; through the real served app it is an integration
case, pending on H1 like the rest.
"""

import io
from types import SimpleNamespace
from typing import Any, Final

import pytest
from fastapi import FastAPI
from pypdf import PdfReader

from tests.redteam.standins import ALL_WRITTEN, CategoryLookup, filled
from tests.redteam.suite import CASES, DATASET_DIR, GENERATOR, label_of
from vectapilot_api.extraction.text import read_pages
from vectapilot_api.guards.hidden import hidden_runs
from vectapilot_api.guards.types import (
    UNCHANGED,
    DecidedBy,
    Finding,
    GuardAction,
    GuardDecision,
    GuardRecord,
    RouteSlug,
    TermCategory,
    emit_guard_record,
)
from vectapilot_api.redteam.cases import DocumentFixture, HiddenLine, PageFixture
from vectapilot_api.redteam.document import (
    VEHICLE_REFUSED,
    document_runner,
    extraction_record,
    outcome_of,
    run_document,
)
from vectapilot_api.redteam.harness import RunEnv, Runner, Scored, run_case
from vectapilot_api.redteam.pdf import document_pdf
from vectapilot_api.redteam.plan import CasePath
from vectapilot_api.redteam.scoring import ExpectationMiss, PendingHandWrite, Verdict

ENV: Final = RunEnv(GENERATOR, "in_process", dataset_dir=DATASET_DIR)
DOCUMENT_CASES: Final = ("d-001", "d-002", "d-003", "d-004", "d-005")


def pdf_of(case_id: str) -> bytes:
    case = filled(case_id)
    assert case.case.fixture.document is not None
    return document_pdf(case.case.fixture.document, case.text)


def page_texts(case_id: str) -> list[str]:
    return [page.text for page in read_pages(pdf_of(case_id)).pages]


def scored(case_id: str, runner: Runner) -> Scored:
    return run_case(
        CASES[case_id],
        label_of(case_id),
        ENV,
        runners={CasePath.DOCUMENT: runner},
        probes=ALL_WRITTEN,
    )


# ── The PDF ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("case_id", DOCUMENT_CASES)
def test_each_case_builds_the_same_pdf_whose_text_layer_holds_every_line(case_id: str) -> None:
    document = CASES[case_id].fixture.document
    assert document is not None
    data = pdf_of(case_id)

    assert data == pdf_of(case_id)  # deterministic: no timestamp, no file id
    texts = page_texts(case_id)
    assert len(texts) == len(document.pages)
    for text, page in zip(texts, document.pages, strict=True):
        for line in (*page.visible, *(hidden.text for hidden in page.hidden)):
            assert line in text  # hidden or not, the text layer holds it (the screens read it)


@pytest.mark.parametrize("case_id", DOCUMENT_CASES)
def test_the_operator_walk_finds_exactly_the_hidden_lines(case_id: str) -> None:
    document = CASES[case_id].fixture.document
    assert document is not None
    reader = PdfReader(io.BytesIO(pdf_of(case_id)))

    counted = [hidden_runs(page, reader) for page in reader.pages]

    assert counted == [len(page.hidden) for page in document.pages]


def test_every_hidden_mode_is_found_and_a_visible_line_never_is() -> None:
    lines = ("Visible line", "Second visible line")
    for mode in ("invisible_render", "white_fill", "off_page"):
        page = PageFixture(visible=lines, hidden=(HiddenLine(text="Hidden line", mode=mode),))
        data = document_pdf(DocumentFixture(pages=(page,)), str)
        reader = PdfReader(io.BytesIO(data))
        assert hidden_runs(reader.pages[0], reader) == 1, mode
    plain = document_pdf(DocumentFixture(pages=(PageFixture(visible=lines),)), str)
    reader = PdfReader(io.BytesIO(plain))
    assert hidden_runs(reader.pages[0], reader) == 0


def test_a_line_outside_winansi_is_refused_never_half_built() -> None:
    page = PageFixture(visible=("Telugu తెలుగు",))
    with pytest.raises(ValueError):  # UnicodeEncodeError
        document_pdf(DocumentFixture(pages=(page,)), str)


@pytest.mark.parametrize("line", ["Smile :)", "Open ( bracket", "Back\\slash"])
def test_a_line_with_a_bracket_or_a_backslash_is_written_whole(line: str) -> None:
    data = document_pdf(DocumentFixture(pages=(PageFixture(visible=(line,)),)), str)
    [page] = read_pages(data).pages
    assert line in page.text


def test_two_extraction_records_for_one_upload_are_refused() -> None:
    one = {"route": "extraction", "decided_by": "none"}
    with pytest.raises(ValueError, match="more than one"):
        extraction_record([one, dict(one)])  # G10: one record per request
    assert extraction_record([{"route": "agent"}, one]) == one
    assert extraction_record([{"route": "agent"}]) is None


# ── With H1's stub ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("case_id", DOCUMENT_CASES)
def test_with_h1s_stub_the_screen_fails_closed_and_the_case_is_pending_on_h1(
    case_id: str,
) -> None:
    result = run_document(filled(case_id), ENV)

    assert (result.outcome, result.decided_by, result.error_type) == (
        "unavailable_503",
        DecidedBy.GUARD_ERROR,
        "NotImplementedError",
    )
    with pytest.raises(PendingHandWrite) as pending:
        run_case(CASES[case_id], label_of(case_id), ENV)
    assert pending.value.reason == "pending:H1"


# ── With a stand-in for H1 ───────────────────────────────────────────────────────────────────


def lookup(case_id: str, categories: dict[int, TermCategory]) -> CategoryLookup:
    """NOT H1: page `n`'s whole text (as the screen reads it) -> a category."""
    texts = page_texts(case_id)
    return CategoryLookup({texts[n]: category for n, category in categories.items()})


INJECTION, EMERGENCY = TermCategory.INJECTION, TermCategory.EMERGENCY


@pytest.mark.parametrize(
    ("case_id", "categories", "findings"),
    [
        ("d-001", {0: INJECTION}, {Finding.DOCUMENT_HIDDEN_TEXT, Finding.INJECTION_PHRASE}),
        ("d-002", {0: INJECTION}, {Finding.DOCUMENT_HIDDEN_TEXT, Finding.INJECTION_PHRASE}),
        ("d-003", {0: EMERGENCY}, {Finding.DOCUMENT_HIDDEN_TEXT, Finding.RED_FLAG_EMERGENCY}),
        ("d-004", {1: INJECTION}, {Finding.INJECTION_PHRASE}),
    ],
)
def test_a_flagged_page_is_allowed_and_flagged_with_ds_findings(
    case_id: str, categories: dict[int, TermCategory], findings: set[Finding]
) -> None:
    result = scored(case_id, document_runner(lookup(case_id, categories)))

    assert result.verdict is Verdict.PASSED
    assert result.result.outcome == "allowed"
    assert result.result.decided_by is DecidedBy.NONE  # G7: a flag, never a decision
    assert result.result.findings == frozenset(findings)


def test_the_off_page_red_flag_needs_the_hidden_text_signal_as_well() -> None:
    # d-003's scorer wants both findings: the same lines drawn visibly, with the red flag found,
    # are a miss, never a pass on the red flag alone (ananya's PR 2 review).
    document = CASES["d-003"].fixture.document
    assert document is not None
    page = document.pages[0]
    shown = DocumentFixture(
        pages=(PageFixture(visible=(*page.visible, *(line.text for line in page.hidden))),)
    )
    [text] = [p.text for p in read_pages(document_pdf(shown, str)).pages]
    fixture = CASES["d-003"].fixture.model_copy(update={"document": shown})
    case = CASES["d-003"].model_copy(update={"fixture": fixture})
    runner = document_runner(CategoryLookup({text: EMERGENCY}))

    with pytest.raises(ExpectationMiss):
        run_case(case, "hard", ENV, runners={CasePath.DOCUMENT: runner}, probes=ALL_WRITTEN)


def test_a_red_flag_the_screen_misses_is_a_miss() -> None:
    with pytest.raises(ExpectationMiss):
        scored("d-003", document_runner(CategoryLookup({})))


def test_d_005_the_forced_review_stays_a_miss_until_pr_11() -> None:
    # The screen flags the instructions page; nothing hands the upload off (G7, no forced review
    # before PR 11), so d-005's `handoff` is a miss: its strict known_gap xfail holds.
    assert label_of("d-005") == "known_gap"
    runner = document_runner(lookup("d-005", {1: INJECTION}))

    with pytest.raises(ExpectationMiss):
        scored("d-005", runner)


# ── The database environment's plumbing, on a fake served suite ──────────────────────────────

STORED_ID: Final = "00000000-0000-0000-0000-000000000007"


class FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body


class FakeClient:
    """Answers one upload with `status` and writes the route's `guard` record, as the app would."""

    def __init__(self, status: int, decision: GuardDecision) -> None:
        self.status = status
        self.decision = decision
        self.posts: list[tuple[str, dict[str, str], dict[str, str], bytes]] = []

    def post(
        self, url: str, *, content: bytes, params: dict[str, str], headers: dict[str, str]
    ) -> FakeResponse:
        self.posts.append((url, dict(params), dict(headers), content))
        emit_guard_record(
            GuardRecord(
                business_id=headers["X-Business-Id"],
                trace_id="0123456789abcdef" * 2,
                route=RouteSlug.EXTRACTION,
                rules_version=None,
                decision=self.decision,
                checks=(),
                findings={Finding.DOCUMENT_HIDDEN_TEXT: 1},
            )
        )
        body = {"id": STORED_ID} if self.status == 201 else {"error": "x"}
        return FakeResponse(self.status, body)


def fake_served(client: FakeClient, *, stored: object) -> SimpleNamespace:
    app = FastAPI()
    kinds = {"referral_letter": object()}
    app.state.extraction = SimpleNamespace(
        businesses={"sunrise-clinic": SimpleNamespace(kinds=kinds)}
    )
    return SimpleNamespace(app=app, client=client, stored_extractions=stored)


def test_through_the_route_a_stored_upload_is_handed_back_for_removal() -> None:
    client = FakeClient(201, UNCHANGED)
    stored: list[tuple[str, str]] = []
    env = RunEnv(GENERATOR, "database", fake_served(client, stored=stored), DATASET_DIR)

    result = run_document(filled("d-001"), env)

    assert (result.outcome, result.decided_by) == ("allowed", DecidedBy.NONE)
    assert result.findings == frozenset({Finding.DOCUMENT_HIDDEN_TEXT})
    assert stored == [("sunrise-clinic", STORED_ID)]
    [(url, params, headers, content)] = client.posts
    assert (url, params) == ("/v1/extractions", {"kind": "referral_letter"})
    assert headers == {"X-Business-Id": "sunrise-clinic", "Content-Type": "application/pdf"}
    assert content == pdf_of("d-001")


def test_through_the_route_a_503_stores_nothing_and_needs_the_suites_list() -> None:
    client = FakeClient(503, GuardDecision(GuardAction.UNAVAILABLE, DecidedBy.GUARD_ERROR))
    stored: list[tuple[str, str]] = []
    env = RunEnv(GENERATOR, "database", fake_served(client, stored=stored), DATASET_DIR)

    result = run_document(filled("d-001"), env)

    assert (result.outcome, result.decided_by) == ("unavailable_503", DecidedBy.GUARD_ERROR)
    assert stored == []
    no_list = RunEnv(GENERATOR, "database", fake_served(client, stored=None), DATASET_DIR)
    with pytest.raises(ValueError, match="take back"):
        run_document(filled("d-001"), no_list)
    with pytest.raises(ValueError, match="served app"):
        run_document(filled("d-001"), RunEnv(GENERATOR, "database", object(), DATASET_DIR))


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(201, "allowed"), (422, "rejected_422"), (429, "blocked_429"), (503, "unavailable_503")],
)
def test_the_routes_status_is_the_outcome(status: int, outcome: str) -> None:
    assert outcome_of(status) == outcome
    assert outcome_of(500) == "server_error"


@pytest.mark.parametrize("status", [404, 413, 415])
def test_a_refused_document_is_a_broken_vehicle_never_a_score(status: int) -> None:
    with pytest.raises(ValueError, match=VEHICLE_REFUSED):
        outcome_of(status)


def test_a_document_case_without_its_document_is_refused() -> None:
    case = CASES["d-001"].model_copy(
        update={"fixture": CASES["d-001"].fixture.model_copy(update={"document": None})}
    )
    with pytest.raises(ValueError, match="needs its document"):
        run_case(case, "hard", ENV, probes=ALL_WRITTEN)
    with pytest.raises(ValueError, match="dataset folder"):
        run_document(filled("d-002"), RunEnv(GENERATOR, "in_process"))

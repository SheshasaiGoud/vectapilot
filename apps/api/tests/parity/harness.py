"""The widget's red-team parity harness (M8 PR 2a; S6, S7, S11). Test code only: no harness package
changes (S13; `vectapilot_api.redteam` is read, never edited).

**One app, two routes, the same pipeline.** `parity_app` serves the red-team world's folders
(`redteam.businesses.write_app_folders`: the sample businesses and `rt-shop`, with the guard
folder) and a widget folder holding the two sample widget files and a synthetic one for `rt-shop`.
For each case, the agent runner's own builder (`redteam.agent.build_pipeline`, the in-process
world: the fixture tools, a memory store, an in-memory limiter armed for the case's outage) makes
a fresh pipeline **per route**, and `redteam.agent.overriding` answers the three dependency
functions both routes share (S6: `get_agent_guard`, `get_agent_service`,
`get_read_only_agent_service`). So `/v1/agent` and `/v1/widget/messages` run the same guard and
services, built the same way, and differ only in what the widget adds: the session, the outer
bound, the turn row and the hand-off store (an in-memory `RecordingHandoffStore`).

- `/v1/agent`: `X-Business-Id` and the case's seeded customer, as M7's runner sends it (the header
  route's business lookup answers "registered": no database).
- `/v1/widget/messages`: a bearer token; `get_customer_session` answers a session of the case's
  business with its own `s-<hex>` reference (S4). The customer reference differs from the agent
  route's, as it always will: the fixture tools ignore it, and the limiter is in memory.
- **C's ticket text** (c-007, c-008; PR 2b, widget confirm): when the turn withheld a poisoned
  ticket (G7: stored already expired, its token never returned), the runner confirms that real
  token as M7's runner does, through each route's own confirm route
  (`/v1/agent/actions/{id}/confirm` with the header and the customer, `/v1/widget/actions/{id}/
  confirm` with the session), both answered through `get_confirm_service` by the real
  `ConfirmService` over a scripted connection whose one row is that withheld action, expired
  (`withheld_service`): the control is the confirm's one 404 on both. Against Postgres it is M7's
  database case (`redteam.agent`'s served runner).

**What is compared** (`Seen.decision`): the status, and for a 200 the turn's kind, reason,
priority, intent and guidance, a reply's text, an answer's outcome; for a 429 the body; the
`guard` record's `decided_by`, action, error type and findings; for a ticket-text case, whether
the turn returned a token and the confirm's status and body. A 422 is compared by status (the
session route's closed body differs by design, S1). The outcome code (`redteam.agent.outcome_of`)
is recorded for the parity gate.

**The two runs** (S7): `seams` is the registered H1, H2 and H3 (`redteam.agent.REAL_SEAMS`: while
H1 is a stub, every case is `guard_error` on both routes); `standins` replaces H1 by M7's
`CategoryLookup` (`tests/redteam/standins.py`, NOT H1) driven by the case's own expectation (and,
for C's ticket text, the ticket's summary as an injection, as M7's own served test of c-008 hits
it, `tests/redteam/test_served.py`; for a reply family whose output rule is a phrase check, each
scripted reply as that rule's term category, as M7's runner tests drive I, noah's PR 2 review),
with H2 and H3 as written, so the cases reach their real paths (red flags, the injection screen,
the read-only service, tool-result and chunk screening, output checks, outages) on both routes.
The stand-in run must also reach each case's frozen expectation, but for the gate's closed list
(`scripts.parity_gate.STANDIN_MISSES`): parity alone cannot tell a widget that skips a step
whose case never fires on either route.
"""

import json
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.parity_gate import EXCLUDED, PARITY_ROUTES
from tests.actions.fakes import ScriptedSource, action_row
from tests.conftest import make_settings
from tests.guards.helpers import use_answer_key_selftest
from tests.redteam.standins import CategoryLookup
from tests.redteam.suite import CASES, DATASET, DATASET_DIR, REPO_ROOT
from tests.widget.guarded import RecordingHandoffStore
from tests.widget.helpers import WIDGET_DIR, read_json, write_json
from vectapilot_api.actions.confirm import ConfirmService
from vectapilot_api.actions.router import get_confirm_service
from vectapilot_api.actions.tokens import ConfirmToken, token_sha256
from vectapilot_api.auth.dependencies import get_customer_session
from vectapilot_api.auth.store import CustomerSession, customer_ref_for
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.db import Database
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.guards.output import _PHRASE_FINDINGS
from vectapilot_api.guards.pii import Masked
from vectapilot_api.guards.screens import Masker
from vectapilot_api.guards.types import Clean, TermCategory
from vectapilot_api.main import create_app
from vectapilot_api.redteam import businesses
from vectapilot_api.redteam.agent import (
    CONFIRM_PATH,
    REAL_SEAMS,
    FixtureSource,
    GuardSeams,
    MemoryStore,
    Observed,
    Pipeline,
    agent_record,
    build_pipeline,
    fixture_registry,
    guard_records,
    outcome_of,
    overriding,
    world_of,
)
from vectapilot_api.redteam.cases import Case, Category, Outage, Surface
from vectapilot_api.redteam.harness import FilledCase
from vectapilot_api.redteam.scoring import OUTPUT_FAMILIES
from vectapilot_api.widget.messages import get_handoff_store

ORIGIN: Final = "https://api.vectapilot.example"
AGENT: Final = "/v1/agent"
WIDGET: Final = "/v1/widget/messages"
WIDGET_CONFIRM: Final = "/v1/widget/actions/{action_id}/confirm"
BEARER: Final = {"authorization": "Bearer parity.not-a-token"}
"""Never read: `get_customer_session` is answered by the harness (no database)."""
REPLAYED: Final = tuple(
    sorted(case.id for case in CASES.values() if case.route.value in PARITY_ROUTES)
)
"""The frozen agent-route and confirm-route ids (zara's count: 100 and 2)."""
INCLUDED: Final = tuple(case_id for case_id in REPLAYED if case_id not in EXCLUDED)
RT_SHOP_WIDGET: Final = {
    "business_id": "rt-shop",
    "synthetic": True,
    "review": "TEST-ONLY widget for the red-team fixture business (M8 PR 2a parity)",
    "enabled": True,
    "display_name": "RT Shop (TEST-ONLY)",
    "allowed_origins": ["https://www.rt-shop.example"],
    "languages": ["en"],
    "texts": [
        {"language": "en", "greeting": "TEST-ONLY greeting.", "contact_fallback": ["TEST-ONLY"]}
    ],
}


# ── The app ──────────────────────────────────────────────────────────────────────────────────


def _widget_folder(destination: Path) -> Path:
    folder = destination / "widget"
    (folder / "businesses").mkdir(parents=True)
    for source in sorted((WIDGET_DIR / "businesses").glob("*.json")):
        write_json(folder / "businesses" / source.name, read_json(source))
    write_json(folder / "businesses" / "rt-shop.json", RT_SHOP_WIDGET)
    return folder


@contextmanager
def parity_app() -> Iterator[tuple[FastAPI, TestClient]]:
    """The served app over the red-team world (module docstring), with no database."""
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
        root = Path(directory)
        folders = businesses.write_app_folders(
            root,
            dataset_dir=DATASET_DIR,
            sandbox_dir=REPO_ROOT / "data" / "sandbox",
            corpus_dir=REPO_ROOT / DATASET.manifest.corpus,
            fixture_businesses=DATASET.manifest.fixture_businesses,
            safety_dir=REPO_ROOT / "data" / "safety",
        )
        settings = make_settings(
            tools_config_dir=folders.tools_dir,
            corpus_dir=folders.corpus_dir,
            guard_config_dir=folders.guard_dir,
            widget_config_dir=_widget_folder(root),
            public_origin=ORIGIN,
        )

        async def registered(self: Database, business_id: str) -> bool:
            return True

        patch.setattr(Database, "business_exists", registered)
        use_answer_key_selftest(patch)
        app = create_app(settings, limiter=FakeLimiter())
        with TestClient(app, base_url="https://testserver") as client:
            yield app, client


# ── One case through one route ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Seen:
    """One route's answer to one case: what parity compares, and the codes the gate reports."""

    decision: tuple[object, ...]
    outcome: str
    decided_by: str | None
    stored: RecordingHandoffStore | None = None
    body: Mapping[str, Any] | None = None
    confirm: tuple[object, ...] = ()
    """C's ticket text: `("confirm", status, body)` of the withheld token's confirm, else `()`."""


def _turn_decision(status: int, body: Mapping[str, Any]) -> tuple[object, ...]:
    if status == 422:
        return (status,)
    if status != 200:
        return (status, json.dumps(body, sort_keys=True))
    answer = body.get("answer") if isinstance(body.get("answer"), dict) else {}
    return (
        status,
        body.get("kind"),
        body.get("reason"),
        body.get("priority"),
        body.get("intent"),
        tuple(body.get("guidance", ())),
        body.get("text"),
        answer.get("outcome") if isinstance(answer, dict) else None,
        answer.get("answer") if isinstance(answer, dict) else None,
    )


def _record_decision(record: Mapping[str, object] | None) -> tuple[object, ...]:
    if record is None:
        return (None,)
    findings = record.get("findings")
    return (
        record.get("decided_by"),
        record.get("action"),
        record.get("priority"),
        record.get("guidance"),
        record.get("error_type"),
        tuple(sorted(findings)) if isinstance(findings, dict) else None,
    )


class _RaisingMask:
    """J's `guard_raising`: the masker, a guard after the red-flag screen (G1), raises (as
    M7's runner arms it)."""

    def __call__(self, text: Clean, locales: object) -> Masked:
        raise RuntimeError("an armed guard failure")


def armed(outage: Outage | None, seams: GuardSeams) -> GuardSeams:
    if outage is Outage.GUARD_RAISING:
        return replace(seams, mask=cast(Masker, _RaisingMask()))
    return seams


def limiter_for(outage: Outage | None) -> FakeLimiter:
    """The case's outage on an in-memory limiter, as M7's runner arms it."""
    if outage is Outage.BURST:
        return FakeLimiter(limit="refuse")
    if outage is Outage.BUDGET_EXHAUSTED:
        return FakeLimiter(budget="refuse")
    if outage is Outage.REDIS_DOWN:
        return FakeLimiter(limit="raise", budget="raise")
    return FakeLimiter()


def pipeline_for(filled: FilledCase, seams: GuardSeams, store: MemoryStore) -> Pipeline:
    """A fresh in-process pipeline for one case (module docstring), its proposals in `store`."""
    case = filled.case
    world = world_of(DATASET_DIR)
    config = world.tools[BusinessId(case.business_id)].config
    outage = case.fixture.outage
    return build_pipeline(
        case,
        filled.text,
        world,
        source=FixtureSource(),
        store=store,
        limiter=limiter_for(outage),
        seams=armed(outage, seams),
        registry=fixture_registry(case, filled.text, config),
        failing=outage is Outage.PROVIDER_FAILING,
    )


def session_of(case: Case) -> CustomerSession:
    """The case's own widget session (each case on its own session, S11): an id derived from the
    case id, and its `s-<hex>` reference (S4)."""
    session_id = uuid5(NAMESPACE_URL, f"vectapilot-parity:{case.id}")
    return CustomerSession(
        business_id=BusinessId(case.business_id),
        session_id=session_id,
        customer_ref=customer_ref_for(session_id),
        ui_language="en",
    )


def withheld_service(case: Case, action_id: UUID, token: str, customer: str) -> ConfirmService:
    """The real confirm service over a scripted connection whose one action is the turn's withheld
    ticket, as `MemoryStore.create_withheld` stored it: already expired (G7), with its own token's
    hash, for the confirming customer. Its own rules then decide (W6: expired is the one 404);
    the scripted statements are the action's read and its audit row's insert."""
    config = world_of(DATASET_DIR).tools[BusinessId(case.business_id)].config
    row = action_row(
        business_id=case.business_id,
        id=action_id,
        tool="create_ticket",
        arguments={},
        customer_ref=customer,
        confirm_token_sha256=token_sha256(ConfirmToken(token)),
        expired=True,
    )
    return ConfirmService(ScriptedSource([[row], []]), {case.business_id: config})


def confirm_withheld(
    app: FastAPI, client: TestClient, case: Case, store: MemoryStore, customer: str, *, widget: bool
) -> tuple[object, ...]:
    """C's ticket text: confirm the withheld ticket's real token through the route's own confirm
    route (module docstring); `()` for every other case, or when nothing was withheld."""
    if case.surface is not Surface.TICKET_TEXT or not store.withheld:
        return ()
    action_id, token = store.withheld[0]
    service = withheld_service(case, action_id, token, customer)
    app.dependency_overrides[get_confirm_service] = lambda: service
    try:
        if widget:
            response = client.post(
                WIDGET_CONFIRM.format(action_id=action_id),
                json={"confirm_token": token},
                headers=BEARER,
            )
        else:
            response = client.post(
                CONFIRM_PATH.format(action_id=action_id),
                json={"customer_ref": customer, "confirm_token": token},
                headers={BUSINESS_HEADER: case.business_id},
            )
    finally:
        app.dependency_overrides.pop(get_confirm_service, None)
    return ("confirm", response.status_code, response.content)


def _confirmed(seen: "Seen", body: Mapping[str, Any], confirm: tuple[object, ...]) -> "Seen":
    if not confirm:
        return seen
    return replace(seen, decision=(*seen.decision, "confirm_token" in body, *confirm))


def through_agent(app: FastAPI, client: TestClient, filled: FilledCase, seams: GuardSeams) -> Seen:
    case = filled.case
    store = MemoryStore()
    pipeline = pipeline_for(filled, seams, store)
    customer = businesses.customer_ref_for(
        case, world_of(DATASET_DIR).tools[BusinessId(case.business_id)].seed
    )
    with overriding(app, pipeline), guard_records() as records:
        response = client.post(
            AGENT,
            json={"message": filled.input, "customer_ref": customer},
            headers={BUSINESS_HEADER: case.business_id},
        )
    body = response.json()
    seen = _seen(response.status_code, body, agent_record(records), filled, pipeline)
    confirm = confirm_withheld(app, client, case, store, customer, widget=False)
    return replace(_confirmed(seen, body, confirm), confirm=confirm)


def through_widget(
    app: FastAPI,
    client: TestClient,
    filled: FilledCase,
    seams: GuardSeams,
    *,
    message: str | None = None,
    store: RecordingHandoffStore | None = None,
) -> Seen:
    """`message` (the case's input by default) through `/v1/widget/messages` on the case's own
    session, with the hand-off store in memory (`store`, fresh by default)."""
    case = filled.case
    proposals = MemoryStore()
    pipeline = pipeline_for(filled, seams, proposals)
    stored = store or RecordingHandoffStore()
    session = session_of(case)
    extra = {get_customer_session: lambda: session, get_handoff_store: lambda: stored}
    app.dependency_overrides.update(extra)
    try:
        with overriding(app, pipeline), guard_records() as records:
            response = client.post(
                WIDGET, json={"message": message or filled.input}, headers=BEARER
            )
        confirm = confirm_withheld(app, client, case, proposals, session.customer_ref, widget=True)
    finally:
        for dependency in extra:
            app.dependency_overrides.pop(dependency, None)
    body = response.json()
    turn = body["turn"] if response.status_code == 200 else body
    seen = _seen(response.status_code, turn, agent_record(records), filled, pipeline)
    return replace(_confirmed(seen, turn, confirm), stored=stored, body=body, confirm=confirm)


def _seen(
    status: int,
    body: Mapping[str, Any],
    record: Mapping[str, object] | None,
    filled: FilledCase,
    pipeline: Pipeline,
) -> Seen:
    observed = Observed(status, body, record)
    decided = None if record is None else record.get("decided_by")
    return Seen(
        decision=(*_turn_decision(status, body), *_record_decision(record)),
        outcome=outcome_of(observed, filled.input, pipeline.log),
        decided_by=decided if isinstance(decided, str) else None,
    )


# ── The stand-in run's seams (NOT H1) ────────────────────────────────────────────────────────

CATEGORY_OF_EXPECT: Final[Mapping[str, TermCategory]] = {
    "urgent_handoff": TermCategory.EMERGENCY,
    "high_handoff": TermCategory.PROFESSIONAL_JUDGEMENT,
    "write_tools_removed": TermCategory.INJECTION,
}
PHRASE_CATEGORY_OF: Final[Mapping[str, TermCategory]] = {
    finding.value: category for category, finding in _PHRASE_FINDINGS.items()
}
"""H3's phrase step, inverted: the output decision (`decided_by`, equal to the finding's code) a
term category of the reply makes (`guards/output.py`: success claim, medical advice, diagnosis,
abuse). The other output rules (boundary, ungrounded digits, PII) need no matcher hit."""


def reply_category(case: Case) -> TermCategory | None:
    """The term category a reply family's output rule needs from the matcher, or None."""
    decider = OUTPUT_FAMILIES.get(case.family)
    return None if decider is None else PHRASE_CATEGORY_OF.get(decider.value)


def standin_table(filled: FilledCase) -> Mapping[str, TermCategory]:
    """What `CategoryLookup` hits for one case, from the case's own expectation: the message for a
    red flag or an injection, every free-text field of a poisoned tool result (C) and every
    poisoned chunk (B) as an injection. A fixed lookup over whole texts: never a matcher."""
    case = filled.case
    table: dict[str, TermCategory] = {}
    category = CATEGORY_OF_EXPECT.get(case.expect)
    if category is not None:
        table[filled.input] = category
    if case.category is Category.C:
        for result in case.fixture.tool_results:
            for value in result.fields.values():
                if isinstance(value, str):
                    table[filled.text(value)] = TermCategory.INJECTION
    if case.category is Category.B:
        for chunk in case.fixture.chunks:
            table[filled.text(chunk.text)] = TermCategory.INJECTION
    if case.surface is Surface.TICKET_TEXT:  # the ticket's summary, as M7's served c-008 test
        for step in case.fixture.scripted_model:
            arguments = getattr(step, "arguments", None) or {}
            if getattr(step, "tool", None) == "create_ticket" and "summary" in arguments:
                table[filled.text(str(arguments["summary"]))] = TermCategory.INJECTION
    phrase = reply_category(case)
    if phrase is not None:  # each scripted reply, as M7's runner tests drive I (noah)
        for step in case.fixture.scripted_model:
            reply = getattr(step, "text", None)
            if isinstance(reply, str):
                table[filled.text(reply)] = phrase
    return table


def standin_seams(filled: FilledCase) -> GuardSeams:
    """The stand-in run's seams: `CategoryLookup` for H1, H2's `mask_pii` and H3's `check_reply`
    as written (`redteam.agent.REAL_SEAMS`'s)."""
    return replace(REAL_SEAMS, matcher=CategoryLookup(standin_table(filled)))


def seams_for(run: str, filled: FilledCase) -> GuardSeams:
    return standin_seams(filled) if run == "standins" else REAL_SEAMS

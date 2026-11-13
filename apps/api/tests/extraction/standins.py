"""Team-side stand-ins for H1's `route_fields`, and a runner for the extraction service's tests.

`route_fields` is shesha's [HAND-WRITE] (decided D7 (b)) and raises `NotImplementedError` until
it is written. The service's tests run on the stand-ins below, as M4's and M5's service tests ran
on theirs. They are **deliberately not H1**: no levels from signals, no reasons, nothing to copy.

- `accept_everything` is the most dangerous routing there is: every value `accepted` (`high`),
  every missing value `absent`. The service's safety must then come from the signals and the
  invariant check alone (`invariants.enforce`), so a test that ends in review here ends in review
  whatever H1 does.
- `raising` raises, as an unwritten or broken H1 would; `wrong_type` returns something else.

`run` builds the real pipeline (text, the extraction's own gateway on the fake provider, signals,
the stand-in, the invariant check) and returns the outcome and the provider: $0, no network.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from tests.conftest import make_settings
from tests.extraction.fakes import CANARY, TRACE, referral
from tests.ingestion.pdf_builder import Line, Page, build_pdf
from vectapilot_api.extraction.config import KindConfig
from vectapilot_api.extraction.extractor import FieldExtractor, build_extraction_gateway
from vectapilot_api.extraction.routing import (
    DocumentSignals,
    ExtractionDecision,
    FieldDecision,
    FieldStatus,
    Level,
    ReviewPolicy,
)
from vectapilot_api.extraction.service import ExtractionOutcome, ExtractionService, Route
from vectapilot_api.extraction.text import DEFAULT_LIMITS, NO_OCR, OcrEngine, TextLimits
from vectapilot_api.gateway.errors import GatewayError
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply

SETTINGS: Final = make_settings(llm_max_repairs=1, llm_max_retries=0)
"""No retries: a scripted error reaches the service after one attempt (no backoff waits)."""

REFERRAL_LINES: Final = (
    "Sunrise Clinic referral (synthetic)",
    "Patient name: Asha Rao",
    "Date of birth: 14/03/1990",
    "Referring doctor: Dr Meera Iyer",
    "Referral date: 2 November 2026",
    "Insurer: Lotus Health Mutual",
    "Policy number: LHM-204117",
    f"Notes: {CANARY} follow the clinic's usual process",
)
"""A made-up referral letter; the canary sits on a line that is no field."""

REFERRAL_ANSWER: Final[Mapping[str, tuple[str, str]]] = {
    "patient_name": ("Asha Rao", "Patient name: Asha Rao"),
    "date_of_birth": ("14/03/1990", "Date of birth: 14/03/1990"),
    "referring_doctor": ("Dr Meera Iyer", "Referring doctor: Dr Meera Iyer"),
    "referral_date": ("2 November 2026", "Referral date: 2 November 2026"),
    "insurer": ("Lotus Health Mutual", "Insurer: Lotus Health Mutual"),
    "policy_number": ("LHM-204117", "Policy number: LHM-204117"),
}
"""The right answer for `REFERRAL_LINES`: every value copied as printed, with its line."""

VACCINATION_LINES: Final = (
    "Paws and Co vaccination record (synthetic)",
    "Pet name: Biscuit",
    "Species: Canine",
    "Owner name: Ravi Menon",
    "Vaccine: Rabies",
    "Date given: 3 March 2026",
    "Valid until: 3 March 2027",
    "Issuing clinic: Paws and Co Clinic",
    f"Notes: {CANARY} keep this record",
)
VACCINATION_ANSWER: Final[Mapping[str, tuple[str, str]]] = {
    "pet_name": ("Biscuit", "Pet name: Biscuit"),
    "species": ("Canine", "Species: Canine"),
    "owner_name": ("Ravi Menon", "Owner name: Ravi Menon"),
    "vaccine": ("Rabies", "Vaccine: Rabies"),
    "date_given": ("3 March 2026", "Date given: 3 March 2026"),
    "valid_until": ("3 March 2027", "Valid until: 3 March 2027"),
    "issuing_clinic": ("Paws and Co Clinic", "Issuing clinic: Paws and Co Clinic"),
}

Proposed = tuple[str | None, str | None] | tuple[str | None, str | None, bool]


def answer(kind: KindConfig, fields: Mapping[str, Proposed]) -> dict[str, Any]:
    """A shape-valid answer for `kind`: each field `(value, quote[, unsure])`, others null."""
    body: dict[str, Any] = {}
    for spec in kind.template.fields:
        given = fields.get(spec.name, (None, None))
        unsure = given[2] if len(given) == 3 else False
        body[spec.name] = {"value": given[0], "quote": given[1], "unsure": unsure}
    return body


def reply(kind: KindConfig, fields: Mapping[str, Proposed]) -> FakeReply:
    return FakeReply(text=json.dumps(answer(kind, fields)))


def with_changes(
    base: Mapping[str, tuple[str, str]], **changes: Proposed | None
) -> dict[str, Proposed]:
    """`base` with fields changed (`None` removes a field: the model proposes null)."""
    fields: dict[str, Proposed] = dict(base)
    for name, change in changes.items():
        if change is None:
            fields.pop(name, None)
        else:
            fields[name] = change
    return fields


# ── Stand-ins for `route_fields` (not H1) ─────────────────────────────────────────────────────


def accept_everything(doc: DocumentSignals, policy: ReviewPolicy) -> ExtractionDecision:
    """Every value accepted, every missing value absent, whatever the signals say."""
    with_value = {signals.name for signals in doc.fields if signals.has_value}
    return ExtractionDecision.of(
        [
            FieldDecision(name, FieldStatus.ACCEPTED, Level.HIGH)
            if name in with_value
            else FieldDecision(name, FieldStatus.ABSENT)
            for name in policy.fields
        ]
    )


def raising(doc: DocumentSignals, policy: ReviewPolicy) -> ExtractionDecision:
    raise RuntimeError(f"routing broke on {CANARY}")  # its text must never be logged


def wrong_type(doc: DocumentSignals, policy: ReviewPolicy) -> ExtractionDecision:
    return "accepted"  # type: ignore[return-value]


# ── The runner ────────────────────────────────────────────────────────────────────────────────


def pdf(*pages: Sequence[str | Line] | Page) -> bytes:
    return build_pdf(list(pages))


async def run(
    data: bytes,
    *script: FakeReply | GatewayError,
    kind: KindConfig | None = None,
    route: Route = accept_everything,
    ocr: OcrEngine = NO_OCR,
    limits: TextLimits = DEFAULT_LIMITS,
) -> tuple[ExtractionOutcome, FakeProvider]:
    """The service on the fake provider with `script` (default kind: Sunrise's referral)."""
    chosen = referral() if kind is None else kind
    provider = FakeProvider(list(script))
    async with build_extraction_gateway(SETTINGS, provider=provider) as gateway:
        service = ExtractionService(FieldExtractor(gateway), route=route, ocr=ocr, limits=limits)
        outcome = await service.extract(
            chosen, data, business_id=chosen.business_id, trace_id=TRACE
        )
    return outcome, provider

"""M6 PR 2, E9: nothing under `extraction/` can put document text into a log line or an error.

The plan's "No leaks", three ways:
1. **A grep over the package's source:** no `exc_info=True`, no `logger.exception` (both write the
   traceback and the message, which may quote a document), and no `str(...)`, `repr(...)` or
   f-string of a caught exception. Exception *types* are fine (`type(error).__name__`).
2. **A seeded stdlib loop of malformed values through every parser** (`signals.py`): nothing
   escapes, and no check fails closed by accident (`SignalReport.errors` stays empty). Seeded,
   so a failure reproduces; stdlib only (`hypothesis` is not in `uv.lock`, plan "Review notes").
3. **Canaries:** every fixture document holds `CANARY` on a line that is no field; it never
   reaches a log record (formatted as production formats it, `exception` included), an
   `llm_call` record, an exception's text or the outcome, on success and on every failure path.
"""

import json
import logging
import random
import re
import string
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

from tests.extraction.fakes import CANARY, TRACE, referral, vaccination
from tests.extraction.standins import (
    REFERRAL_ANSWER,
    REFERRAL_LINES,
    accept_everything,
    pdf,
    raising,
    reply,
    run,
)
from tests.ingestion.pdf_builder import Page
from vectapilot_api import extraction
from vectapilot_api.extraction import signals
from vectapilot_api.extraction.config import OUTPUT_SLACK, DateFormat
from vectapilot_api.extraction.service import EXTRACTION, ExtractionOutcome, Route
from vectapilot_api.extraction.signals import canon
from vectapilot_api.extraction.text import DocumentRefused, DocumentText, PageText
from vectapilot_api.gateway.errors import GatewayError, Transient
from vectapilot_api.gateway.gateway import LLM_CALL
from vectapilot_api.gateway.providers.fake import INVALID_JSON_TEXT, FakeReply
from vectapilot_api.log import JsonFormatter

PACKAGE: Final = Path(extraction.__file__).parent
FORBIDDEN: Final = {
    "exc_info=True": re.compile(r"exc_info\s*=\s*True"),
    "logger.exception": re.compile(r"\.exception\("),
    "format_exc": re.compile(r"format_exc|print_exc|format_exception"),
    "str(error)": re.compile(r"\b(?:str|repr)\(\s*(?:error|exc|exception|err|e)\s*\)"),
    "f-string of an error": re.compile(r"\{(?:error|exc|exception|err|e)(?:!r|!s)?\}"),
}

ALLOWED: Final = frozenset(
    {
        # PR 1, reviewed: our own `AgentConfigError` at startup names a manifest and a rule, never
        # a value or a document (`files.load_business`).
        ("files.py", "problem = str(error)  # names the manifest and the rule, never a value"),
    }
)
"""Exact (file, stripped line) pairs a reviewer accepted; anything else matching fails."""


def sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_the_package_has_sources() -> None:
    assert {path.name for path in sources()} >= {
        "text.py",
        "prompt.py",
        "extractor.py",
        "signals.py",
        "invariants.py",
        "service.py",
    }


@pytest.mark.parametrize("name", sorted(FORBIDDEN))
def test_no_source_under_extraction_formats_an_exception(name: str) -> None:
    found = [
        f"{path.name}:{number}"
        for path in sources()
        for number, line in enumerate(path.read_text("utf-8").splitlines(), start=1)
        if FORBIDDEN[name].search(line) and (path.name, line.strip()) not in ALLOWED
    ]
    assert found == [], f"{name} under extraction/ (E9): {found}"


def test_every_allowed_line_still_exists() -> None:
    # An allowance outlives its line only by mistake: remove it with the line.
    lines = {
        (path.name, line.strip())
        for path in sources()
        for line in path.read_text("utf-8").splitlines()
    }
    assert lines >= ALLOWED


@pytest.mark.parametrize(
    ("name", "line"),
    [
        ("exc_info=True", 'logger.info("x", exc_info=True)'),
        ("logger.exception", 'logger.exception("x")'),
        ("format_exc", "traceback.format_exc()"),
        ("str(error)", "detail = str(error)"),
        ("f-string of an error", 'f"failed: {exc!r}"'),
    ],
)
def test_each_pattern_catches_what_it_names(name: str, line: str) -> None:
    assert FORBIDDEN[name].search(line)


# ── 2. A seeded stdlib loop through every parser ───────────────────────────────────────────────

SEED: Final = 20261112
ROUNDS: Final = 400
ALPHABET: Final = (
    string.ascii_letters
    + string.digits
    + " ./-:,;'&()<>[]{}\\*+?^$|#\n\t"
    + "".join(
        chr(code)
        for code in (
            0x00,  # NUL
            0xDF,  # sharp s: case folding lengthens it
            0xE9,  # e acute
            0x130,  # dotted capital I: case folding lengthens it
            0x301,  # a combining accent
            0x663,  # an Arabic-Indic digit
            0x200B,  # a zero-width space
            0x2019,  # a typographic apostrophe
            0x2013,  # an en dash
            0xFB01,  # the fi ligature (NFKC splits it)
            0xFF11,  # a fullwidth digit (NFKC folds it)
            0x1F600,  # an emoji
        )
    )
)
SHAPES: Final = (
    "14/03/1990",
    "5/1/2026",
    "2 November 2026",
    "November 2, 2026",
    "1990-03-14",
    "99/99/9999",
    "Policy number: ",
    "Patient name: ",
    "Species: ",
    "LHM-204117",
)
"""Fragments that reach the parsers' interesting branches (dates, labels, identifiers)."""


def malformed(rng: random.Random, longest: int = 60) -> str:
    parts = [
        rng.choice(SHAPES)
        if rng.random() < 0.3
        else "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 12)))
        for _ in range(rng.randint(0, 5))
    ]
    return "".join(parts)[: rng.randint(0, longest)]


def test_malformed_values_never_escape_a_parser() -> None:
    rng = random.Random(SEED)  # noqa: S311 (a seeded test input, not crypto)
    kind = vaccination()
    policy = referral().template.field("policy_number")
    name = referral().template.field("patient_name")
    species = kind.template.field("species")
    assert policy is not None and name is not None and species is not None
    formats = tuple(DateFormat)
    for _ in range(ROUNDS):
        value, other = malformed(rng), malformed(rng, 200)
        quote = canon(other)
        for text in (value, other):
            signals.plain(text), canon(text), signals.trim(text), signals.name_allowed(text)
            signals.date_tokens(canon(text))
        signals.token_runs(canon(value), quote)
        signals.identifier_runs(canon(value), quote)
        label = signals.find_label([value or "x"], quote)
        signals.ambiguous(signals.parse_date(canon(value), formats))
        signals.check_date(value, quote, formats)
        signals.check_identifier(value, quote, policy)
        signals.check_enum(value, quote, species, kind)
        checked = signals.check_text(value, quote, name, label)
        signals.stored_span(quote, label, checked.place)
        lines = "\n".join(canon(line) for line in other.split("\n"))
        places = signals.label_places(lines, {"a": [value], "b": ["x", "Patient name"]})
        signals.label_readings([lines], [places], "a")
        signals.quote_facts(canon(value), [lines], [places], "a")
        signals.label_present([quote], [value or "x"])


def test_malformed_answers_and_pages_never_fail_a_check() -> None:
    rng = random.Random(SEED + 1)  # noqa: S311 (a seeded test input, not crypto)
    for kind in (referral(), vaccination()):
        for _ in range(ROUNDS // 4):
            page_texts = [malformed(rng, 300) for _ in range(rng.randint(1, 3))]
            body: dict[str, Any] = {}
            for spec in kind.template.fields:
                page = rng.choice(page_texts)
                start = rng.randint(0, len(page))
                quote = rng.choice([malformed(rng, 120), page[start : start + 40], None])
                body[spec.name] = {
                    "value": rng.choice([malformed(rng, OUTPUT_SLACK * spec.value_bound), None]),
                    "quote": quote,
                    "unsure": rng.random() < 0.2,
                }
            proposal = kind.output_model.model_validate(body)
            document = DocumentText(
                tuple(
                    PageText(n, text, has_images=rng.random() < 0.2)
                    for n, text in enumerate(page_texts, start=1)
                ),
                "0" * 64,
            )
            report = signals.compute_signals(kind, document, proposal)
            assert report.errors == ()
            assert [s.name for s in report.document.fields] == [
                spec.name for spec in kind.template.fields
            ]


# ── 3. Canaries ────────────────────────────────────────────────────────────────────────────────

FORMATTER: Final = JsonFormatter()


def leaked(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every captured record as production writes it (JSON, `exception` included), if it holds
    the canary in any case."""
    return [
        record.getMessage()
        for record in caplog.records
        if CANARY.casefold() in FORMATTER.format(record).casefold()
    ]


def extraction_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [getattr(r, EXTRACTION) for r in caplog.records if r.getMessage() == EXTRACTION]


def outcome_text(outcome: ExtractionOutcome) -> str:
    values = [(f.value, f.span) for f in outcome.fields]
    return f"{outcome!r} {values} {outcome.signals!r}"


RIGHT: Final = reply(referral(), dict(REFERRAL_ANSWER))
SCENARIOS: Final[dict[str, tuple[list[FakeReply | GatewayError], Route]]] = {
    "accepted": ([RIGHT], accept_everything),
    "outage": ([Transient(status_code=529)], accept_everything),
    "invalid_twice": (
        [FakeReply(text=INVALID_JSON_TEXT), FakeReply(text=f'{{"{CANARY}": 1}}')],
        accept_everything,
    ),
    "truncated": (
        [FakeReply(text=f'{{"patient_name": "{CANARY}', stop_reason="max_tokens")],
        accept_everything,
    ),
    "routing_raises": ([RIGHT], raising),
}


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_the_canary_never_reaches_a_log_an_llm_call_or_the_outcome(
    scenario: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    script, route = SCENARIOS[scenario]

    outcome, _ = await run(pdf(list(REFERRAL_LINES)), *script, route=route)

    assert leaked(caplog) == []
    calls = [getattr(r, LLM_CALL) for r in caplog.records if r.getMessage() == LLM_CALL]
    assert calls and all(CANARY not in json.dumps(call).casefold() for call in calls)
    assert CANARY not in outcome_text(outcome).casefold()
    [record] = extraction_records(caplog)
    assert record["trace_id"] == TRACE and record["fields"] == len(REFERRAL_ANSWER)


@pytest.mark.anyio
async def test_a_raising_ocr_engines_text_never_reaches_a_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Broken:
        def recognize(self, document: bytes, page_number: int) -> str | None:
            raise OSError(CANARY)

    caplog.set_level(logging.DEBUG)
    outcome, _ = await run(pdf(Page(lines=[], image=True)), ocr=Broken())
    assert leaked(caplog) == []
    assert outcome.error == "OSError" and CANARY not in outcome_text(outcome)


@pytest.mark.anyio
async def test_a_refused_documents_text_never_reaches_a_log_or_the_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    lines = [*REFERRAL_LINES, *(["Filler " + "x" * 93] * 210)]
    with pytest.raises(DocumentRefused) as caught:
        await run(pdf(lines))
    assert CANARY not in str(caught.value)
    assert leaked(caplog) == []
    [record] = extraction_records(caplog)
    assert (record["status"], record["refused"]) == ("refused", "too_much_text")


@pytest.mark.anyio
async def test_the_extraction_record_holds_counts_never_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    await run(pdf(list(REFERRAL_LINES)), RIGHT)
    [record] = extraction_records(caplog)
    assert set(record) == {
        "business_id",
        "trace_id",
        "kind",
        "template_version",
        "prompt_version",
        "rules_version",
        "status",
        "refused",
        "failure",
        "error",
        "pages",
        "image_pages",
        "fields",
        "by_status",
        "by_level",
        "by_reason",
        "routing_invariant",
        "withheld",
        "unverified",
        "signal_errors",
        "signal_error_types",
        "duration_ms",
    }
    assert record["by_status"] == {"accepted": 5, "review": 1}
    assert record["routing_invariant"] == 1  # the stand-in accepted the always-review identifier
    assert (record["withheld"], record["unverified"]) == (0, 0)
    assert (record["signal_errors"], record["signal_error_types"]) == (0, [])
    for value, _ in REFERRAL_ANSWER.values():
        assert value.casefold() not in json.dumps(record).casefold()


@pytest.mark.anyio
async def test_a_broken_check_is_counted_by_type_in_the_record(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As test_signals.py's broken check: its field goes to review, and the record tells a broken
    # check (type names only, E9) from a routing slip.
    def broken(*args: Any) -> Any:
        raise KeyError(CANARY)

    monkeypatch.setattr(signals, "check_identifier", broken)
    caplog.set_level(logging.INFO)
    outcome, _ = await run(pdf(list(REFERRAL_LINES)), RIGHT)
    [record] = extraction_records(caplog)
    assert outcome.signal_errors == ("KeyError",)
    assert outcome.field_of("policy_number").status.value == "review"
    assert (record["signal_errors"], record["signal_error_types"]) == (1, ["KeyError"])
    assert CANARY not in json.dumps(record) and CANARY not in caplog.text


@pytest.mark.anyio
async def test_signal_error_types_are_sorted_and_each_listed_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two date checks break with one type, the identifier check with another: the count is 3,
    # the types are the sorted distinct names (never in field order, never repeated).
    def raises(error: type[Exception]) -> Callable[..., Any]:
        def broken(*args: Any) -> Any:
            raise error(CANARY)

        return broken

    monkeypatch.setattr(signals, "check_date", raises(ValueError))
    monkeypatch.setattr(signals, "check_identifier", raises(KeyError))
    caplog.set_level(logging.INFO)
    outcome, _ = await run(pdf(list(REFERRAL_LINES)), RIGHT)
    [record] = extraction_records(caplog)
    assert outcome.signal_errors == ("ValueError", "ValueError", "KeyError")
    assert (record["signal_errors"], record["signal_error_types"]) == (
        3,
        ["KeyError", "ValueError"],
    )
    assert CANARY not in json.dumps(record) and CANARY not in caplog.text

"""G10's log redaction (M7 PR 3): `guards.redact`, unit.

**Maskers here are stand-ins, never H2.** `DIGITS` replaces every ASCII digit with `#`, `HOSTILE`
replaces any text it is given with `[MASKED]` (so a field that reaches the masker at all shows
it), `RAISING` always raises. H2's own contract is tests/guards/test_mask_pii.py. With H2 a stub,
`log_masker()` is None and the app's formatter writes free text as before (the interim,
`guards/redact.py`'s docstring): tested here directly, and through every route in
tests/guards/test_log_canary.py.

Personal data comes from the red-team generator (`redteam/pii.py`, reserved ranges) or is a
string of all ones or nines; the canary mark is free text with no PII shape.
"""

import asyncio
import contextlib
import io
import json
import logging
import subprocess
import sys
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any, Final, NoReturn

import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter
from starlette.routing import Route

from tests.conftest import make_settings
from tests.gateway.test_gateway import Harness, request
from vectapilot_api.agent import service as agent_service
from vectapilot_api.answer import service as answer_service
from vectapilot_api.embeddings import runner
from vectapilot_api.extraction import router as extraction_router
from vectapilot_api.extraction import service as extraction_service
from vectapilot_api.gateway import gateway
from vectapilot_api.gateway import types as gateway_types
from vectapilot_api.gateway.errors import GatewayError
from vectapilot_api.gateway.providers.fake import FakeReply
from vectapilot_api.gateway.types import SLUG_PATTERN, BusinessId, CallRecord
from vectapilot_api.guards import redact
from vectapilot_api.guards.locales import MASK_CAP
from vectapilot_api.guards.normalize import clean
from vectapilot_api.guards.redact import (
    EXCEPTION_LINES,
    FIELD_CAP,
    ID_FORMATS,
    METADATA_RECORDS,
    OTHER,
    RAW_CAP,
    REDACTION_FAILED,
    TRUNCATED,
    UNMATCHED,
    AccessLine,
    RedactingJsonFormatter,
    RequestScope,
    frames_only,
    log_masker,
    route_template,
    safe_cut,
)
from vectapilot_api.guards.types import GUARD_RECORD
from vectapilot_api.log import LOG_MASKING, DropQueryString, JsonFormatter, configure_logging
from vectapilot_api.main import create_app
from vectapilot_api.measure import records
from vectapilot_api.measure.records import parse_llm_call_lines
from vectapilot_api.redteam.pii import LABEL_OF, PiiGenerator, PiiType, Placeholder
from vectapilot_api.tools import executor

MARK: Final = "zq7canary4e1b"
PHONE: Final = "+91 00000 20777"
EMAIL: Final = "m7.canary@example.com"
TRACE: Final = "4bf92f3577b34da6a3ce929d0e0e4736"
ACTION: Final = "8d0e2f1c-3b4a-4c5d-9e6f-7a8b9c0d1e2f"
SHA: Final = "ab" * 32


def digits(text: str) -> str:
    """A stand-in masker, NOT H2: every ASCII digit becomes `#`."""
    return "".join("#" if c.isascii() and c.isdigit() else c for c in text)


def hostile(text: str) -> str:
    """A stand-in masker, NOT H2: whatever it is given becomes `[MASKED]`."""
    return "[MASKED]"


def raising(text: str) -> NoReturn:
    raise RuntimeError(f"masker failed on {text}")  # its message holds the text: never written


DIGITS: Final = RedactingJsonFormatter(digits)
HOSTILE: Final = RedactingJsonFormatter(hostile)
RAISING: Final = RedactingJsonFormatter(raising)
INTERIM: Final = RedactingJsonFormatter(None)


def make(
    msg: object = "hello",
    args: Any = None,
    *,
    name: str = "vectapilot_api",
    exc_info: Any = None,
    **extra: object,
) -> logging.LogRecord:
    record = logging.LogRecord(name, logging.INFO, __file__, 1, msg, args, exc_info)
    record.__dict__.update(extra)  # what `logger.info(..., extra={...})` does
    return record


def loads(line: str) -> dict[str, Any]:
    data = json.loads(line)
    assert isinstance(data, dict)
    return data


# ── Metadata records pass by key ─────────────────────────────────────────────────────────────


def test_the_metadata_records_are_the_ones_the_code_writes() -> None:
    written = {
        records.LLM_CALL,
        gateway.LLM_CALL,
        runner.EMBEDDING_CALL,
        answer_service.RAG_ANSWER,
        agent_service.AGENT_TURN,
        executor.TOOL_CALL,
        extraction_service.EXTRACTION,
        extraction_router.NOT_STORED,
        GUARD_RECORD,
    }
    assert written == METADATA_RECORDS


def llm_call_records() -> list[logging.LogRecord]:
    """The gateway's own `llm_call` records (fake provider): ok with a trace id, a refusal, a
    truncation, a tool-use stop and an unknown alias (no attempt, no model)."""
    captured: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    async def calls() -> None:
        h = Harness(
            [
                FakeReply(),
                FakeReply(stop_reason="refusal"),
                FakeReply(stop_reason="max_tokens"),
                FakeReply(stop_reason="tool_use"),
            ]
        )
        for req in (
            request(trace_id=TRACE),
            request(),
            request(),
            request(),
            request(alias="no-such-alias"),
        ):
            with contextlib.suppress(GatewayError):  # a refusal or a truncation raises
                await h.gateway.complete(req)

    logger = logging.getLogger("vectapilot_api.gateway")
    handler = Capture()
    logger.addHandler(handler)
    try:
        asyncio.run(calls())
    finally:
        logger.removeHandler(handler)
    return [r for r in captured if r.getMessage() == gateway.LLM_CALL]


@pytest.mark.parametrize("formatter", [HOSTILE, DIGITS, RAISING, INTERIM], ids=str)
def test_llm_call_lines_are_byte_identical_and_parse_to_the_same_records(
    formatter: RedactingJsonFormatter,
) -> None:
    # M3's window: the redacted line is today's line, whatever the masker would do to it.
    calls = llm_call_records()
    assert len(calls) == 5
    plain = [JsonFormatter().format(r) for r in calls]

    redacted = [formatter.format(r) for r in calls]

    assert redacted == plain
    parsed = parse_llm_call_lines(redacted)
    assert parsed == parse_llm_call_lines(plain)
    assert parsed == tuple(CallRecord.model_validate(getattr(r, gateway.LLM_CALL)) for r in calls)
    assert parsed[0].trace_id == TRACE
    assert parsed[-1].model_requested is None  # the unknown alias's record


METADATA_SAMPLES: Final[dict[str, dict[str, object]]] = {
    "agent_turn": {
        "business_id": "corner-shop",
        "trace_id": TRACE,
        "prompt_version": "agent.1",
        "kind": "confirmation",
        "action_id": ACTION,
        "router_artifact": SHA,
        "steps": 2,
        "error": None,
        "latency_ms": 12.5,
    },
    "tool_call": {
        "business_id": "paws-and-co",
        "tool": "order_status",
        "outcome": "ok",
        "duration_ms": 3.25,
        "trace_id": None,
        "action_id": None,
        "audited": True,
    },
    "guard": {
        "business_id": "sunrise-clinic",
        "trace_id": TRACE,
        "guidance": "Call the clinic on +91 00000 11111 or 108 now.",  # the business's own
        "findings": {"pii": 2},
        "withheld_action_id": ACTION,
        "durations_ms": {"pii": 0.5},
    },
    "extraction": {
        "business_id": "sunrise-clinic",
        "trace_id": TRACE,
        "by_status": {"review": 3},
        "signal_error_types": ["ValueError"],
        "duration_ms": 9.0,
    },
    "rag_answer": {"business_id": "paws-and-co", "cost_usd": "0.0000123456789012"},
    "embedding_call": {"business_id": "paws-and-co", "request_id": "req_1111111111111111"},
    "extraction_not_stored": {"business_id": "sunrise-clinic", "trace_id": TRACE},
    "llm_call": {"business_id": "paws-and-co", "request_ids": ["req_9999999999999999"]},
}


@pytest.mark.parametrize("name", sorted(METADATA_RECORDS))
def test_a_metadata_record_never_reaches_the_masker(name: str) -> None:
    # Digit runs a card or phone pattern would take (a cost, a request id, the business's own
    # guidance) stay as they are: the record is metadata by construction.
    record = make(name, name=f"vectapilot_api.{name}", **{name: METADATA_SAMPLES[name]})

    assert HOSTILE.format(record) == JsonFormatter().format(record)


def test_a_metadata_record_still_has_its_ids_checked() -> None:
    bad = {"trace_id": f"{MARK} not an id", "action_id": "nine 99999 99999", "steps": 1}
    record = make("agent_turn", name="vectapilot_api.agent", agent_turn={"run": [bad]})

    line = loads(HOSTILE.format(record))

    assert line["agent_turn"] == {
        "run": [{"trace_id": REDACTION_FAILED, "action_id": REDACTION_FAILED, "steps": 1}]
    }


@pytest.mark.parametrize(
    ("name", "msg", "args", "extra"),
    [
        ("uvicorn.error", "llm_call", None, {"llm_call": {"note": MARK}}),  # not our logger
        ("vectapilot_api", "llm_call %s", (MARK,), {"llm_call": {"note": MARK}}),  # has args
        # Args that leave the message as it is: still not a metadata record.
        ("vectapilot_api", "llm_call", ({"x": 1},), {"llm_call": {"note": MARK}}),
        ("vectapilot_api", "llm_call", None, {"llm_call": MARK}),  # not a dict
        ("vectapilot_api", "a_new_record", None, {"a_new_record": {"note": MARK}}),  # unknown
        ("vectapilot_apis", "llm_call", None, {"llm_call": {"note": MARK}}),  # another package
    ],
)
def test_anything_else_is_free_text(
    name: str, msg: str, args: Any, extra: dict[str, object]
) -> None:
    line = HOSTILE.format(make(msg, args, name=name, **extra))

    assert MARK not in line
    assert loads(line)["message"] == "[MASKED]"


# ── Free text ────────────────────────────────────────────────────────────────────────────────


def test_the_message_and_every_free_text_extra_are_masked() -> None:
    record = make(
        "call %s",
        (PHONE,),
        note=f"write to {EMAIL} or 9999999999",
        count=9999999999,
        ratio=0.5,
        flag=True,
        empty=None,
        nested={"items": [PHONE, {"deep": "1111 1111 1111"}], "ok": "no digits"},
        amount=Decimal("1111.11"),
        pair=(1, "2"),
    )

    line = loads(DIGITS.format(record))

    assert line["message"] == "call +## ##### #####"
    assert line["note"] == f"write to {digits(EMAIL)} or ##########"
    assert line["count"] == "##########"  # a number is written as digits: masked as text
    assert line["ratio"] == "#.#"
    assert (line["flag"], line["empty"]) == (True, None)
    assert line["nested"] == {
        "items": ["+## ##### #####", {"deep": "#### #### ####"}],
        "ok": "no digits",
    }
    assert line["amount"] == "####.##"  # `str()`, as `json.dumps(default=str)` would
    assert line["pair"] == ["#", "#"]


def test_a_number_that_masks_to_itself_stays_a_number() -> None:
    line = loads(RedactingJsonFormatter(lambda text: text).format(make(count=3, ratio=0.5)))

    assert (line["count"], line["ratio"]) == (3, 0.5)


def test_id_fields_pass_unchanged_when_valid_and_fail_closed_when_not() -> None:
    record = make(
        trace_id=TRACE,
        business_id="sunrise-clinic",
        nested={"action_id": ACTION, "withheld_action_id": None, "router_artifact": SHA},
        bad={"trace_id": TRACE.upper(), "business_id": f"Not-A-Business {MARK}"},
        action_id=9,
    )

    line = loads(HOSTILE.format(record))

    assert (line["trace_id"], line["business_id"]) == (TRACE, "sunrise-clinic")
    assert line["nested"] == {
        "action_id": ACTION,
        "withheld_action_id": None,
        "router_artifact": SHA,
    }
    assert line["bad"] == {"trace_id": REDACTION_FAILED, "business_id": REDACTION_FAILED}
    assert line["action_id"] == REDACTION_FAILED


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("trace_id", TRACE + "919999999999"),  # still all hex
        ("trace_id", f"{TRACE} call {PHONE}"),
        ("action_id", ACTION + "99999"),
        ("action_id", f"{ACTION} {MARK}"),
        ("router_artifact", SHA + "99"),
        ("business_id", "a" * 41),
        ("business_id", f"sunrise-clinic\n{MARK}"),
    ],
)
def test_an_id_with_anything_after_it_fails_closed(key: str, value: str) -> None:
    # An id field is the one free-text path that skips masking: the whole value must be the id
    # (zara's PR 3 mutation check: a valid id followed by a phone number was written in full).
    line = loads(HOSTILE.format(make(**{key: value}, nested={key: value})))

    assert (line[key], line["nested"]) == (REDACTION_FAILED, {key: REDACTION_FAILED})


def test_a_uuid_object_is_a_valid_action_id_and_passes_unchanged() -> None:
    # diego's PR 3 review: a record that logs `action.id` itself must not lose it.
    action = uuid.UUID(ACTION)
    record = make("tool_call", tool_call={"action_id": action}, withheld_action_id=action)

    for formatter in (INTERIM, HOSTILE):
        assert formatter.format(record) == JsonFormatter().format(record)
    # Only the UUID keys take one: as a trace id its text has hyphens.
    assert loads(HOSTILE.format(make(trace_id=action)))["trace_id"] == REDACTION_FAILED


@pytest.mark.parametrize("text", ["ab", "abc", "a" * 40, "a" * 41, "sunrise-clinic", "a--b", "A-b"])
def test_the_business_id_format_is_the_gateways_own(text: str) -> None:
    # Copied, not imported (`gateway.types` is a leaf module, but the formatter stays out of the
    # gateway package): pinned here, so a change to `BusinessId` cannot silently fail the
    # byte-identity of real `llm_call` lines.
    try:
        TypeAdapter(BusinessId).validate_python(text)
    except ValueError:
        valid = False
    else:
        valid = True

    assert bool(ID_FORMATS["business_id"].fullmatch(text)) is valid
    assert SLUG_PATTERN in ID_FORMATS["business_id"].pattern
    assert ID_FORMATS["trace_id"].pattern == gateway_types._TRACE_ID_RE.pattern


@pytest.mark.parametrize(
    ("value", "written"),
    [
        ({f"{MARK} key": 1}, REDACTION_FAILED),  # a key can hold text too
        ({1: "one"}, REDACTION_FAILED),
        ({"": "empty"}, REDACTION_FAILED),
        ([[[[[[[[[MARK]]]]]]]]], [[[[[[[[REDACTION_FAILED]]]]]]]]),
        ([MARK] * (redact.MAX_ITEMS + 1), REDACTION_FAILED),
    ],
    ids=["text-key", "int-key", "empty-key", "deep", "wide"],
)
def test_a_container_it_cannot_walk_fails_closed(value: object, written: object) -> None:
    line = loads(HOSTILE.format(make(note=value)))

    assert line["note"] == written
    assert MARK not in json.dumps(line)


def test_a_container_of_exactly_max_items_is_walked() -> None:
    line = loads(HOSTILE.format(make(note=["x"] * redact.MAX_ITEMS)))

    assert line["note"] == ["[MASKED]"] * redact.MAX_ITEMS


def test_a_failing_masker_fails_each_field_closed_and_the_line_survives() -> None:
    record = make("call %s", (PHONE,), note=EMAIL, trace_id=TRACE, count=1)

    line = loads(RAISING.format(record))

    assert line["message"] == line["note"] == line["count"] == REDACTION_FAILED
    assert line["trace_id"] == TRACE
    assert "masker failed" not in json.dumps(line)


def no_text(text: str) -> Any:
    return None


def test_a_masker_that_returns_no_text_fails_closed() -> None:
    formatter = RedactingJsonFormatter(no_text)

    assert loads(formatter.format(make(note="x")))["note"] == REDACTION_FAILED


class Unprintable:
    def __str__(self) -> str:
        raise RuntimeError(MARK)


def test_a_value_that_cannot_be_turned_into_text_fails_closed() -> None:
    line = loads(HOSTILE.format(make(note=Unprintable(), other="kept")))

    assert (line["note"], line["other"]) == (REDACTION_FAILED, "[MASKED]")


def test_the_formatter_never_raises_and_never_writes_the_record() -> None:
    # `getMessage` fails (a `%d` for text): logging's own error path would print the arguments.
    record = make("%d calls", (PHONE,))

    line = loads(DIGITS.format(record))

    assert line == {
        "time": line["time"],
        "level": "INFO",
        "logger": "vectapilot_api",
        "message": REDACTION_FAILED,
    }
    assert JsonFormatter().format(make("plain")).startswith('{"time": ')


def test_without_a_masker_free_text_is_written_as_today() -> None:
    # The interim: H2 is the stub, so `log_masker()` is None. Free text is not masked or cleaned.
    record = make("call %s\t", (PHONE,), note=EMAIL * 200, count=9, trace_id=TRACE)

    assert INTERIM.format(record) == JsonFormatter().format(record)


def test_without_a_masker_the_container_bounds_already_apply() -> None:
    # diego's PR 3 review: not every line is today's. A dict keyed by a number, which
    # `JsonFormatter` writes, fails closed already; no record we write has one.
    record = make(counts={1: 2})

    assert loads(JsonFormatter().format(record))["counts"] == {"1": 2}
    assert loads(INTERIM.format(record))["counts"] == REDACTION_FAILED


# ── The cap ──────────────────────────────────────────────────────────────────────────────────


def test_a_field_over_the_cap_is_cut_at_a_safe_space_then_masked() -> None:
    words = "word " * (FIELD_CAP // 5 + 10)

    line = loads(DIGITS.format(make(note=f"{words}tail 99")))

    assert FIELD_CAP - 5 <= len(line["note"]) - len(TRUNCATED) - 1 <= FIELD_CAP
    assert line["note"].endswith(f"word {TRUNCATED}")
    assert FIELD_CAP == 2_000 < MASK_CAP


class Recording:
    """A stand-in masker, NOT H2: returns its text as it is and keeps what it was given."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, text: str) -> str:
        self.seen.append(text)
        return text


@pytest.mark.parametrize("invisible", ["\u200b", "\u2060", "\ufeff", "\u00ad"])
def test_the_cut_reads_the_cleaned_text_the_masker_masks(invisible: str) -> None:
    # diego's PR 3 review: in "99999<ZW> <ZW>99999" the raw space has no digit beside it, but
    # `clean` drops the invisible characters and the masker would see "99999 99999" cut after
    # its first half. Cleaned first, that space is between digits: never a place to cut. (ZWJ
    # and ZWNJ survive `clean`; digits joined by them are a masking gap, guide §2.6.)
    value = f"99999{invisible} {invisible}99999"
    for length in range(FIELD_CAP - 16, FIELD_CAP - 2):  # the cap falls across the value
        recording = Recording()
        RedactingJsonFormatter(recording).format(make(note=f"{'a' * length} b {value} tail"))
        [given] = recording.seen[1:]  # [0] is the message
        assert "9" not in given, length
        assert given == "a" * length


def test_a_field_longer_than_the_raw_cap_never_keeps_its_cleaned_end() -> None:
    recording = Recording()
    formatter = RedactingJsonFormatter(recording)
    pad = "\u200b" * (RAW_CAP - 12)  # `clean` drops it all

    # What was cleaned ends inside a value: the kept text ends before it.
    assert loads(formatter.format(make(note=f"{pad}say hi 99999 99999")))["note"] == (
        f"say {TRUNCATED}"
    )
    # No safe place before the end: fail closed.
    assert loads(formatter.format(make(note=f"{pad}say 99999 9999")))["note"] == REDACTION_FAILED
    assert loads(formatter.format(make(note="\u200b" * (RAW_CAP + 5))))["note"] == (
        REDACTION_FAILED
    )
    # Long text: cut at a safe space, as below the raw cap.
    words = loads(formatter.format(make(note="word " * (RAW_CAP // 5 + 10))))["note"]
    assert words.endswith(f"word {TRUNCATED}")
    assert len(words) <= FIELD_CAP + len(TRUNCATED) + 1


def test_a_field_with_no_safe_place_to_cut_fails_closed() -> None:
    assert loads(DIGITS.format(make(note="9 " * FIELD_CAP)))["note"] == REDACTION_FAILED
    assert loads(DIGITS.format(make(note="x" * (FIELD_CAP + 1))))["note"] == REDACTION_FAILED


def test_safe_cut_leaves_a_fitting_text_alone() -> None:
    text = "a b" * 10
    assert safe_cut(text, len(text)) is text


@pytest.mark.parametrize(
    ("text", "cap", "cut"),
    [
        ("a" * 10 + " b", 10, "a" * 10),  # the space right at the cap
        ("ab c9 d", 5, "ab"),  # a digit before the space
        ("ab c 9d", 5, "ab"),  # a digit after it
        ("ab c +d", 5, "ab"),  # a plus after it
        ("ab c \uff0bd", 5, "ab"),  # a fullwidth plus, read through the view
        ("ab c \u096fd", 5, "ab"),  # a Devanagari digit, the same
    ],
)
def test_safe_cut_takes_the_last_space_with_no_number_beside_it(
    text: str, cap: int, cut: str
) -> None:
    assert safe_cut(text, cap) == cut


GENERATED: Final = [
    pytest.param(kind, PiiGenerator().value("t-cut", Placeholder(kind, 1)), id=kind.value)
    for kind in PiiType
]


@pytest.mark.parametrize(("kind", "value"), GENERATED)
def test_the_cut_never_leaves_part_of_a_value(kind: PiiType, value: str) -> None:
    # Wherever the cap falls inside the value, the kept text ends before the value starts.
    assert kind in LABEL_OF
    prefix = "see " * 20
    start = len(prefix)
    text = f"{prefix}{value} and more words after it"
    for cap in range(start + 1, start + len(value)):
        cut = safe_cut(text, cap)
        assert cut is not None
        assert len(cut) < start, (kind, cap - start)


# ── Exceptions (G10-S1) ──────────────────────────────────────────────────────────────────────


def raised[E: BaseException](exc: E) -> E:
    """`exc` after it was raised, so it has a traceback."""
    try:
        raise exc
    except BaseException:  # a test helper: returned, never swallowed
        return exc


def chained() -> BaseException:
    """`KeyError(MARK)` raised from a `ValueError` raised while handling an `OSError`: every
    message holds the canary, and one has a note."""
    try:
        try:
            raise OSError(f"disk {PHONE}")
        except OSError:
            raise ValueError(f"bad {EMAIL}")  # noqa: B904 - an implicit context, on purpose
    except ValueError as inner:
        error = KeyError(MARK)
        error.add_note(f"note {MARK}")
        try:
            raise error from inner
        except KeyError as caught:
            return caught


def test_the_servers_error_line_keeps_class_names_and_frames_only() -> None:
    exc = chained()
    record = make(
        "Exception in ASGI application\n", name="uvicorn.error", exc_info=(type(exc), exc, None)
    )

    for formatter in (INTERIM, DIGITS):
        exception = loads(formatter.format(record))["exception"]
        for part in (MARK, "00000", EMAIL):  # a frame's source line is code, never a value
            assert part not in exception
        assert exception.count("Traceback (most recent call last):") == 3
        assert "test_redact.py" in exception and "in chained" in exception
        assert "\nOSError\n" in exception
        assert "\nValueError\n" in exception
        assert exception.endswith("\nKeyError")
        assert "direct cause" in exception and "During handling" in exception


def test_frames_only_follows_pythons_own_order_and_links() -> None:
    exc = chained()

    lines = [line for line in frames_only(exc).splitlines() if not line.startswith(" ")]

    assert lines == [
        "Traceback (most recent call last):",
        "OSError",
        "",
        "During handling of the above exception, another exception occurred:",
        "",
        "Traceback (most recent call last):",
        "ValueError",
        "",
        "The above exception was the direct cause of the following exception:",
        "",
        "Traceback (most recent call last):",
        "KeyError",
    ]


def test_frames_only_names_a_module_class_and_drops_a_suppressed_context() -> None:
    try:
        try:
            raise OSError(MARK)
        except OSError:
            raise json.JSONDecodeError(MARK, MARK, 0) from None
    except json.JSONDecodeError as caught:
        text = frames_only(caught)

    assert text.endswith("\njson.decoder.JSONDecodeError")
    assert "OSError" not in text and MARK not in text


def test_frames_only_writes_a_group_s_sub_exceptions_by_class() -> None:
    subs = [ValueError(f"{MARK} {n}") for n in range(redact.MAX_GROUP + 2)]
    group = raised(ExceptionGroup(f"group {MARK}", [raised(e) for e in subs]))

    text = frames_only(group)

    assert MARK not in text
    assert text.count("\nValueError") == redact.MAX_GROUP
    assert "ExceptionGroup" in text and "and 2 more" in text


def test_a_small_nested_group_is_written_whole() -> None:
    inner = raised(ExceptionGroup(MARK, [raised(KeyError(MARK))]))
    group = raised(BaseExceptionGroup(MARK, [inner, raised(KeyboardInterrupt(MARK))]))

    text = frames_only(group)

    assert MARK not in text and "more" not in text
    assert [line for line in text.splitlines() if not line.startswith((" ", "Traceback"))] == [
        "BaseExceptionGroup",
        "+---------------- 1 ----------------",
        "ExceptionGroup",
        "+---------------- 1 ----------------",
        "KeyError",
        "+---------------- 2 ----------------",
        "KeyboardInterrupt",
    ]


def test_frames_only_survives_a_cycle_and_a_missing_exception() -> None:
    first, second = ValueError(MARK), TypeError(MARK)
    first.__context__, second.__context__ = second, first

    text = frames_only(first)

    assert MARK not in text and text.count("Error") == 2
    assert frames_only(None) == ""


def test_a_syntax_error_s_source_text_is_never_written() -> None:
    exc = raised(SyntaxError(MARK, ("<input>", 1, 1, f"{MARK} = 1", 1, 2)))

    assert frames_only(exc).endswith("\nSyntaxError")
    assert MARK not in frames_only(exc)


def deep(n: int) -> None:
    if n == 0:
        raise ValueError(MARK)
    deep(n - 1)


def test_a_deep_traceback_keeps_its_last_frames() -> None:
    # The last `MAX_FRAMES` frames, where the exception was raised (a negative limit, as
    # `traceback`'s own), never the first ones.
    try:
        deep(redact.MAX_FRAMES + 10)
    except ValueError as caught:
        text = frames_only(caught)

    assert "in test_a_deep_traceback_keeps_its_last_frames" not in text
    assert "raise ValueError(MARK)" in text  # the source line of the last frame


class Boom(Exception):
    """An exception class as a script's own (`python script.py`) would be."""

    __module__ = "__main__"


def test_a_main_module_class_is_written_bare_as_python_does() -> None:
    assert frames_only(Boom(MARK)) == "Boom"


def test_every_exception_line_keeps_its_indentation() -> None:
    # diego's PR 3 review: once H2 masks, each line goes through `clean`, which strips leading
    # whitespace; frame lines and source lines would lose the indentation that tells them apart.
    masker = RedactingJsonFormatter(lambda text: clean(text))  # NOT H2: cleans only
    exc = raised(KeyError(MARK))

    server = make(
        "Exception in ASGI application", name="uvicorn.error", exc_info=(KeyError, exc, None)
    )
    ours = make("planted", exc_info=(KeyError, exc, exc.__traceback__))

    assert loads(masker.format(server))["exception"] == frames_only(exc)
    assert loads(masker.format(ours))["exception"] == frames_only(exc)
    assert any(line.startswith("    ") for line in frames_only(exc).splitlines())
    assert masker.line("    ") == "    "


@pytest.mark.parametrize("name", ["vectapilot_api", "asyncio", "pydantic", "uvicorn.error"])
def test_every_loggers_exception_is_class_names_and_frames_only(name: str) -> None:
    # Decided 2026-11-18 (shesha): D-error-line widened from uvicorn.error to every record with
    # `exc_info` (asyncio's "Task exception was never retrieved", a library's ValidationError
    # with `input_value=...`): masking removes shapes only, so names and symptoms would stay.
    exc = raised(RuntimeError(f"failed for {MARK} at {PHONE}"))
    record = make("planted", name=name, exc_info=(type(exc), exc, exc.__traceback__))

    for formatter in (INTERIM, DIGITS):
        exception = loads(formatter.format(record))["exception"]
        assert exception == frames_only(exc) or formatter is DIGITS
        assert MARK not in exception and "00000" not in exception
        assert exception.endswith("RuntimeError")


def test_a_long_exception_keeps_its_last_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    # `frames_only` bounds its own output (Python's limits on groups, chains and repeats), so the
    # line cut is a second bound: a stand-in output longer than the cap shows it still holds.
    monkeypatch.setattr(redact, "frames_only", lambda exc: "\n".join(["9"] * 500))
    record = make("x", exc_info=(RuntimeError, RuntimeError("r"), None))

    exception = loads(DIGITS.format(record))["exception"].splitlines()

    assert len(exception) == EXCEPTION_LINES + 1
    assert exception[0] == f"[{500 - EXCEPTION_LINES} lines cut]"
    assert exception[-1] == "#"


def test_an_empty_exc_info_writes_no_exception() -> None:
    record = make("x", exc_info=(RuntimeError, RuntimeError("r"), None))
    assert DIGITS.exception(make("x")) == ""
    assert "exception" in loads(DIGITS.format(record))


# ── The access line (G10-S2) ─────────────────────────────────────────────────────────────────


def access(*args: object) -> logging.LogRecord:
    return make('%s - "%s %s HTTP/%s" %d', args, name="uvicorn.access")


def test_route_template_reads_the_route_then_the_endpoint_then_gives_up() -> None:
    app = create_app(make_settings())
    [docs] = [r for r in app.routes if isinstance(r, Route) and r.path == "/docs"]

    class Named:
        path = "/v1/agent/actions/{action_id}/confirm"

    assert route_template({"route": Named()}) == Named.path
    assert route_template({"endpoint": docs.endpoint, "app": app}) == "/docs"
    assert route_template({"endpoint": lambda: None, "app": app}) == UNMATCHED
    assert route_template({"route": object()}) == UNMATCHED
    assert route_template({}) == UNMATCHED
    assert route_template(None) == UNMATCHED


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("127.0.0.1:5123", "GET", f"/v1/{MARK}?q={MARK}", "1.1", 404), "GET [unmatched]"),
        (("[::1]:5123", "POST", "/v1/x", "1.1", 200), None),
        (("::1:5123", "POST", "/v1/x", "1.1", 200), '::1:5123 - "POST [unmatched] HTTP/1.1" 200'),
        (("", "POST", "/v1/x", "2", 200), ' - "POST [unmatched] HTTP/2" 200'),
        (("127.0.0.1:5123", "+919999999999", "/v1/x", "1.1", 405), f'"{OTHER} [unmatched]'),
        (("127.0.0.1:5123", "GET", "/v1/x", MARK, 200), f"HTTP/{OTHER}"),
        ((f"{MARK}:5123", "GET", "/", "1.1", 200), REDACTION_FAILED),
        (("127.0.0.1:port", "GET", "/", "1.1", 200), REDACTION_FAILED),
        ((None, "GET", "/", "1.1", 200), REDACTION_FAILED),
    ],
)
def test_the_access_line_writes_only_checked_fields(
    args: tuple[object, ...], message: str | None
) -> None:
    record = access(*args)

    assert AccessLine().filter(record) is True

    written = record.getMessage()
    assert MARK not in written and "9999" not in written
    if message is not None:
        assert message in written
    else:
        assert written.startswith(REDACTION_FAILED)  # `[::1]` is not how uvicorn writes it


@pytest.mark.parametrize(
    ("args", "written"),
    [
        (("", "POST", "/v1/x", "2", 200), f' - "POST {UNMATCHED} HTTP/2" 200'),
        # A port of Devanagari digits: `str.isdigit` alone would let the whole field through.
        (
            ("127.0.0.1:" + "\u096f" * 10, "GET", "/", "1.1", 200),
            f'{REDACTION_FAILED} - "GET {UNMATCHED} HTTP/1.1" 200',
        ),
    ],
)
def test_the_access_line_exactly(args: tuple[object, ...], written: str) -> None:
    record = access(*args)

    AccessLine().filter(record)

    assert record.getMessage() == written


@pytest.mark.parametrize(
    "args",
    [
        None,
        ("only", "two"),
        ("127.0.0.1:1", "GET", "/", "1.1", "200"),
        ("1", "G", "/", "1", True),
        ("127.0.0.1:1", "GET", "/", "1.1", 200, MARK),  # one more field than uvicorn's
    ],
)
def test_an_access_record_of_another_shape_keeps_nothing(args: Any) -> None:
    record = access() if args is None else access(*args)
    record.args = args

    assert AccessLine().filter(record) is True
    assert (record.getMessage(), record.args) == (REDACTION_FAILED, None)


def test_the_middleware_names_only_http_scopes_and_create_app_installs_it() -> None:
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(route_template(redact._REQUEST_SCOPE.get()))

    async def call(scope: dict[str, Any]) -> None:
        await RequestScope(app)(scope, None, None)  # type: ignore[arg-type]

    class Named:
        path = "/healthz"

    asyncio.run(call({"type": "lifespan"}))
    asyncio.run(call({"type": "websocket", "route": Named()}))  # a route, but not http
    asyncio.run(call({"type": "http", "route": Named()}))

    assert seen == [UNMATCHED, UNMATCHED, "/healthz"]
    installed: list[object] = [m.cls for m in create_app(make_settings()).user_middleware]
    assert installed == [RequestScope]


# ── The masker and configure_logging ─────────────────────────────────────────────────────────


def test_log_masker_is_none_while_h2_is_the_stub() -> None:
    assert log_masker() is None


def test_log_masker_masks_clean_text_once_h2_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, tuple[str, ...]]] = []

    class Answer:
        def __init__(self, text: str) -> None:
            self.text = text.replace("9", "#")

    def stand_in(text: str, locales: Any) -> Answer:  # NOT H2
        seen.append((text, tuple(locale.id for locale in locales)))
        return Answer(text)

    monkeypatch.setattr(redact, "mask_pii", stand_in)

    masker = log_masker()

    assert masker is not None
    assert masker("call  99​9") == "call ###"  # cleaned first: spaces, the zero-width space
    assert seen[-1] == ("call 999", ("in", "generic"))


def test_a_broken_h2_stops_startup_instead_of_turning_masking_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the stub's `NotImplementedError` means "no masker yet". Any other error from H2 at
    # the startup probe propagates, so `create_app` fails to start (G3: fail closed) instead of
    # logging free text unmasked with only the `masker_unavailable` warning.
    def broken(text: str, locales: Any) -> NoReturn:  # NOT H2
        raise RuntimeError("broken")

    monkeypatch.setattr(redact, "mask_pii", broken)

    with pytest.raises(RuntimeError, match=r"^broken$"):
        log_masker()


@pytest.fixture
def restored() -> Iterator[None]:
    root = logging.getLogger()
    access_logger = logging.getLogger("uvicorn.access")
    saved = root.level, root.handlers[:], access_logger.filters[:]
    try:
        yield
    finally:
        root.setLevel(saved[0])
        root.handlers[:] = saved[1]
        access_logger.filters[:] = saved[2]


def installed() -> tuple[logging.Formatter | None, list[logging.Filter]]:
    [handler] = logging.getLogger().handlers
    filters = logging.getLogger("uvicorn.access").filters
    return handler.formatter, [f for f in filters if isinstance(f, logging.Filter)]


@pytest.mark.usefixtures("restored")
def test_configure_logging_installs_exactly_what_it_is_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    configure_logging("INFO", redact=True)
    configure_logging("INFO", redact=True)
    formatter, filters = installed()
    assert type(formatter) is RedactingJsonFormatter and formatter.masker is None
    assert [type(f) for f in filters if isinstance(f, DropQueryString)] == [AccessLine]

    configure_logging("INFO")
    formatter, filters = installed()
    assert type(formatter) is JsonFormatter  # the M1 CLI's and the MCP server's, unchanged
    assert [type(f) for f in filters if isinstance(f, DropQueryString)] == [DropQueryString]


@pytest.mark.usefixtures("restored")
def test_without_h2_the_redacting_setup_says_so_once_with_a_fixed_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    configure_logging("INFO", redact=True)
    configure_logging("INFO")
    monkeypatch.setattr(redact, "log_masker", lambda: digits)
    configure_logging("INFO", redact=True)

    lines = [loads(line) for line in stdout.getvalue().splitlines()]
    assert lines == [
        {
            "time": lines[0]["time"],
            "level": "WARNING",
            "logger": "vectapilot_api",
            "message": LOG_MASKING,
            "status": "masker_unavailable",
        }
    ]
    formatter, _ = installed()
    assert isinstance(formatter, RedactingJsonFormatter) and formatter.masker is digits


def test_importing_the_log_module_loads_no_guard() -> None:
    # The M1 CLI and the MCP server import `log` for `JsonFormatter`; the guards stay unloaded
    # (PR 6 adds them to the MCP server's never-loaded list).
    code = (
        "import sys\n"
        "from vectapilot_api.log import JsonFormatter, configure_logging\n"
        "configure_logging('INFO')\n"
        "assert not any(m.startswith('vectapilot_api.guards') for m in sys.modules), 'loaded'\n"
    )
    # Our own interpreter and fixed arguments, no shell (S603: the arguments are ours).
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.usefixtures("restored")
def test_the_app_installs_the_redacting_formatter() -> None:
    app: FastAPI = create_app(make_settings())

    [handler] = logging.getLogger().handlers
    assert isinstance(handler.formatter, RedactingJsonFormatter)
    middleware: object = app.user_middleware[0].cls
    assert middleware is RequestScope

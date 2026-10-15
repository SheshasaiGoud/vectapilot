"""Read the gateway's `llm_call` records back and pair each with the call that wrote it (PR 8).

The line format (docs/plans/m1-pr-checks.md, PR 8): one JSON object per line, `"message":
"llm_call"`, and the whole `CallRecord` under the `"llm_call"` key (nested, so it never clashes with
the log formatter's own keys). Other JSON lines (other loggers) are skipped.

The count check: the CLI counts the calls it made and fails the run unless it read exactly that
many `llm_call` records (`join_records`). Each call carries a fresh `trace_id`, which is how a
record is paired with its case. The gateway never adds case metadata; the CLI does (ADR 0007).

Pure: the functions take lines and values, never files. Error messages name a line number or a
case id and the rule, never a line's content.
"""

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vectapilot_api.gateway.types import CallRecord, TraceId

LLM_CALL: Final = "llm_call"
"""The log message and the record's key. Equal to `gateway.gateway.LLM_CALL` (a test pins it);
not imported from there, so reading records never imports the provider SDK."""


class RecordError(ValueError):
    """The records cannot be trusted for this run: unreadable, missing, extra or unpaired."""


class RecordCountMismatch(RecordError):
    """The run read a different number of `llm_call` records than the calls it made."""

    def __init__(self, *, calls_made: int, records_read: int) -> None:
        super().__init__(
            f"expected exactly {calls_made} llm_call records (one per call made),"
            f" read {records_read}: the run fails"
        )
        self.calls_made = calls_made
        self.records_read = records_read


class _Closed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class CallMade(_Closed):
    """One call the CLI made, as the CLI itself recorded it (not from the log)."""

    case_id: str = Field(min_length=1)
    repeat: int = Field(ge=0, strict=True)
    """0 for a case's first run (a distinct prompt); 1, 2, ... for repeats of the same prompt."""
    trace_id: TraceId
    """Fresh for every call (for example `secrets.token_hex(16)`), passed in `LLMRequest`."""


class MeasuredCall(_Closed):
    """A call the CLI made, paired with the one `llm_call` record the gateway wrote for it."""

    case_id: str = Field(min_length=1)
    repeat: int = Field(ge=0, strict=True)
    record: CallRecord


def parse_llm_call_lines(lines: Iterable[str]) -> tuple[CallRecord, ...]:
    """Every `llm_call` record in a stream of JSON log lines, in order.

    Blank lines and JSON objects with another `message` are skipped. A line that is not JSON, an
    `llm_call` line without its record, or a record that is not a valid `CallRecord` raises
    `RecordError` naming the line number.
    """
    records: list[CallRecord] = []
    for number, line in enumerate(lines, start=1):
        if line.strip():
            record = _record_in(line, number)
            if record is not None:
                records.append(record)
    return tuple(records)


_NOT_JSON: Final = object()


def _loads(line: str) -> object:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return _NOT_JSON  # the caller raises, outside the `except`, so nothing is chained


def _record_in(line: str, number: int) -> CallRecord | None:
    payload = _loads(line)
    if payload is _NOT_JSON:
        raise RecordError(f"log line {number} is not JSON")
    if not isinstance(payload, dict) or payload.get("message") != LLM_CALL:
        return None
    try:
        return CallRecord.model_validate(payload.get(LLM_CALL))
    except ValidationError:
        pass  # raised below, outside the `except`, so nothing is chained
    raise RecordError(f"log line {number}: the llm_call record is missing or invalid")


def join_records(
    calls_made: Sequence[CallMade], records: Sequence[CallRecord]
) -> tuple[MeasuredCall, ...]:
    """Pair each call made with its record, by `trace_id`, in the order the calls were made.

    Fails the run (`RecordCountMismatch`) unless there is exactly one record per call made, and
    (`RecordError`) if two calls share a trace id or a `(case_id, repeat)`, or a record has no
    trace id, an unknown one, or one that another record already used.
    """
    if len(records) != len(calls_made):
        raise RecordCountMismatch(calls_made=len(calls_made), records_read=len(records))
    _check_unique(Counter(call.trace_id for call in calls_made), "trace_id among the calls made")
    _check_unique(
        Counter(f"{call.case_id}#{call.repeat}" for call in calls_made), "case_id and repeat"
    )
    by_trace: dict[str, CallRecord] = {}
    for record in records:
        if record.trace_id is None:
            raise RecordError("an llm_call record has no trace_id: it cannot be paired")
        if record.trace_id in by_trace:
            raise RecordError("two llm_call records share a trace_id")
        by_trace[record.trace_id] = record
    measured: list[MeasuredCall] = []
    for call in calls_made:
        paired = by_trace.get(call.trace_id)
        if paired is None:
            raise RecordError(f"no llm_call record for case {call.case_id} (repeat {call.repeat})")
        measured.append(MeasuredCall(case_id=call.case_id, repeat=call.repeat, record=paired))
    return tuple(measured)


def _check_unique(counts: Counter[str], what: str) -> None:
    if any(count > 1 for count in counts.values()):
        raise RecordError(f"every call needs its own {what}")

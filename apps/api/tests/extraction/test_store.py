"""M6 PR 3: the extraction store and the purge, without a database.

- **Caller bugs raise before any statement** (`check_save`): an outcome of another business than
  the one named, a hash that is not one, or differs from the pages' hash, a malformed trace id, a
  retention outside 1-90, a value or span that does not fit its column (`service.storable`).
- **The statements:** every one names `:business_id`; the store inserts only the columns migration
  0007 grants the app role (never an id, `created_at` or `expires_at`); the purge deletes only
  expired rows of one business; nothing selects or writes file bytes.
- **Rows:** reasons in one fixed order, a missing value is no value, E3's all-review rows hold no
  value, span, page or level.
"""

import dataclasses
import importlib.util
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Final, NoReturn, cast
from uuid import UUID

import pytest

from tests.extraction.fakes import SUNRISE_ID, referral
from tests.extraction.served import CREATED, STORED_ID
from vectapilot_api.db import Database
from vectapilot_api.extraction import purge, store
from vectapilot_api.extraction.router import ExtractionResponse
from vectapilot_api.extraction.routing import DocumentStatus, FieldStatus, Level, Reason
from vectapilot_api.extraction.service import (
    STORED_VALUE_MAX,
    ExtractionOutcome,
    FieldResult,
    storable,
)
from vectapilot_api.extraction.signals import SPAN_MAX
from vectapilot_api.extraction.store import (
    PostgresExtractionStore,
    StoredExtraction,
    check_save,
    extraction_row,
    field_row,
    ordered,
)

SHA: Final = "ab" * 32
TRACE: Final = "4bf92f3577b34da6a3ce929d0e0e4736"
MIGRATION: Final = (
    Path(__file__).parents[2] / "migrations" / "versions" / "0007_extraction_tables.py"
)


def migration_0007() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0007", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def outcome(*fields: FieldResult, failure: Reason | None = None) -> ExtractionOutcome:
    in_review = any(result.status is FieldStatus.REVIEW for result in fields)
    return ExtractionOutcome(
        business_id=SUNRISE_ID,
        kind="referral_letter",
        template_version=1,
        status=DocumentStatus.NEEDS_REVIEW if in_review else DocumentStatus.COMPLETE,
        fields=fields,
        sha256=SHA,
        pages=2,
        image_pages=(2,),
        failure=failure,
    )


ACCEPTED: Final = FieldResult(
    "patient_name",
    FieldStatus.ACCEPTED,
    Level.HIGH,
    frozenset(),
    value="Asha Rao",
    span="Patient name: Asha Rao",
    page=1,
)
REVIEWED: Final = FieldResult(
    "insurer",
    FieldStatus.REVIEW,
    Level.MEDIUM,
    frozenset({Reason.IMAGE_ON_PAGE, Reason.UNSURE, Reason.CONFLICTING_VALUES}),
    value="Lotus Health Mutual",
    span=None,
    page=2,
)
ABSENT: Final = FieldResult("referring_doctor", FieldStatus.ABSENT, None, frozenset())


# ── Caller bugs ───────────────────────────────────────────────────────────────────────────────


SAVE: Final[dict[str, object]] = {
    "business_id": SUNRISE_ID,
    "file_sha256": SHA,
    "trace_id": TRACE,
    "retention_days": 30,
}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"business_id": "paws-and-co"}, "another business"),
        ({"file_sha256": "AB" * 32}, "lowercase hex SHA-256"),
        ({"file_sha256": "ab" * 31}, "lowercase hex SHA-256"),
        ({"file_sha256": "ab" * 33}, "lowercase hex SHA-256"),
        ({"file_sha256": "cd" * 32}, "differs from the hash"),
        ({"trace_id": "not-a-trace"}, "W3C trace id"),
        ({"trace_id": TRACE + "0"}, "W3C trace id"),
        ({"trace_id": "0" * 32}, "W3C trace id"),
        ({"retention_days": 0}, "between 1 and 90"),
        ({"retention_days": 91}, "between 1 and 90"),
    ],
)
def test_a_caller_bug_raises_before_any_statement(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_save(outcome(ACCEPTED), **(SAVE | changes))  # type: ignore[arg-type]


@pytest.mark.parametrize("retention_days", [1, 90])
def test_an_outcome_without_read_pages_takes_the_callers_hash(retention_days: int) -> None:
    unread = dataclasses.replace(outcome(ACCEPTED), sha256=None, pages=0)
    check_save(
        unread,
        business_id=SUNRISE_ID,
        file_sha256="cd" * 32,
        trace_id=TRACE,
        retention_days=retention_days,  # both ends of 1-90 are allowed
    )


class NoDatabase:
    """A database whose transaction must never be entered."""

    def __init__(self) -> None:
        self.entered = 0

    @asynccontextmanager
    async def business_transaction(self, business_id: str) -> AsyncIterator[NoReturn]:
        self.entered += 1
        raise AssertionError("a statement was about to be sent")
        yield  # pragma: no cover (makes this a generator)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        # The two rules migration 0007's CHECKs do not repeat: only `check_save` stops them.
        ({"file_sha256": "cd" * 32}, "differs from the hash"),
        ({"trace_id": "0" * 32}, "W3C trace id"),
    ],
)
async def test_save_refuses_a_caller_bug_before_any_transaction(
    changes: dict[str, object], message: str
) -> None:
    database = NoDatabase()
    with pytest.raises(ValueError, match=message):
        await PostgresExtractionStore(cast(Database, database)).save(
            outcome(ACCEPTED),
            **(SAVE | changes),  # type: ignore[arg-type]
        )

    assert database.entered == 0


UNSTORABLE: Final = {
    "a value past the column": {"value": "x" * (STORED_VALUE_MAX + 1)},
    "an empty value": {"value": ""},
    "a value with NUL": {"value": "Asha\x00Rao"},
    "a value with a lone surrogate": {"value": "Asha\ud800"},
    "a span past SPAN_MAX": {"span": "x" * (SPAN_MAX + 1)},
    "an empty span": {"span": ""},
    "a span with NUL": {"span": "Patient name: Asha\x00"},
}
"""Refused by migration 0007 or the driver: a save would be a 503 and lose the upload."""


@pytest.mark.parametrize("change", UNSTORABLE.values(), ids=UNSTORABLE.keys())
def test_a_value_or_span_that_does_not_fit_is_a_caller_bug_never_a_503(
    change: dict[str, str],
) -> None:
    unfit = outcome(dataclasses.replace(ACCEPTED, **change))  # type: ignore[arg-type]

    assert not storable(unfit)
    with pytest.raises(ValueError, match="does not fit its column") as refused:
        check_save(unfit, **SAVE)  # type: ignore[arg-type]
    assert "Asha" not in str(refused.value)  # a fixed message, never the value


def test_the_longest_value_and_span_fit() -> None:
    longest = dataclasses.replace(ACCEPTED, value="x" * STORED_VALUE_MAX, span="y" * SPAN_MAX)

    assert storable(outcome(longest, REVIEWED, ABSENT))
    check_save(outcome(longest), **SAVE)  # type: ignore[arg-type]


# ── The statements ────────────────────────────────────────────────────────────────────────────


def inserted_columns(statement: str) -> tuple[str, ...]:
    match = re.search(r"INSERT INTO \w+ \(([^)]*)\)", statement)
    assert match is not None, statement
    return tuple(column.strip() for column in match[1].split(","))


def test_the_store_inserts_only_the_columns_the_app_role_is_granted() -> None:
    migration = migration_0007()

    assert inserted_columns(str(store.INSERT_EXTRACTION)) == migration.EXTRACTION_INSERT_COLUMNS
    assert inserted_columns(str(store.INSERT_FIELD)) == migration.FIELD_INSERT_COLUMNS


def test_every_statement_names_its_business() -> None:
    for statement in (*store.STATEMENTS, purge.PURGE):
        assert ":business_id" in str(statement), statement


def test_the_purge_deletes_only_one_businesss_expired_extractions() -> None:
    assert str(purge.PURGE) == (
        "DELETE FROM extractions WHERE business_id = :business_id AND expires_at < now()"
    )
    assert {"local", "test", "ci"} == purge.PURGE_ENVIRONMENTS


def test_no_statement_reads_or_writes_file_bytes_or_document_text() -> None:
    for statement in (*store.STATEMENTS, purge.PURGE):
        sql = str(statement).lower()
        assert not any(word in sql for word in ("bytea", "content", "quote", "document")), sql


# ── Rows ──────────────────────────────────────────────────────────────────────────────────────


def test_reasons_are_stored_in_one_fixed_order() -> None:
    assert ordered(REVIEWED.reasons) == ["unsure", "conflicting_values", "image_on_page"]
    assert ordered(frozenset(Reason)) == [reason.value for reason in Reason]


def test_the_rows_hold_the_outcome_and_a_missing_value_is_no_value() -> None:
    result = outcome(ACCEPTED, REVIEWED, ABSENT)
    row = extraction_row(result, file_sha256=SHA, trace_id=TRACE, retention_days=30)
    fields = [field_row(SUNRISE_ID, STORED_ID, r) for r in result.fields]

    assert row == {
        "business_id": SUNRISE_ID,
        "kind": "referral_letter",
        "template_version": 1,
        "prompt_version": "extract-fields.1",
        "rules_version": "extraction-rules.2",
        "status": "needs_review",
        "failure_reason": None,
        "file_sha256": SHA,
        "pages": 2,
        "image_pages": [2],
        "trace_id": TRACE,
        "retention_days": 30,
    }
    assert fields[0] | {"extraction_id": None} == {
        "business_id": SUNRISE_ID,
        "extraction_id": None,
        "field": "patient_name",
        "status": "accepted",
        "level": "high",
        "reasons": [],
        "value": "Asha Rao",
        "span": "Patient name: Asha Rao",
        "page": 1,
    }
    assert fields[1]["reasons"] == ["unsure", "conflicting_values", "image_on_page"]
    assert (fields[2]["level"], fields[2]["value"], fields[2]["span"], fields[2]["page"]) == (
        None,
        None,
        None,
        None,
    )
    assert all(f["extraction_id"] == STORED_ID for f in fields)


def test_an_e3_failure_stores_its_reason_and_no_value() -> None:
    failed = outcome(
        FieldResult("patient_name", FieldStatus.REVIEW, None, frozenset({Reason.TRUNCATED})),
        failure=Reason.TRUNCATED,
    )
    row = extraction_row(failed, file_sha256=SHA, trace_id=TRACE, retention_days=30)

    assert (row["status"], row["failure_reason"]) == ("needs_review", "truncated")
    assert field_row(SUNRISE_ID, STORED_ID, failed.fields[0])["reasons"] == ["truncated"]


def test_the_response_is_the_outcome_with_the_stored_id_and_expiry() -> None:
    stored = StoredExtraction(STORED_ID, CREATED, CREATED)
    response = ExtractionResponse.of(outcome(ACCEPTED, REVIEWED, ABSENT), stored)

    assert response.id == STORED_ID and response.expires_at == CREATED
    assert [f.field for f in response.fields] == ["patient_name", "insurer", "referring_doctor"]
    assert response.fields[1].reasons == [
        Reason.UNSURE,
        Reason.CONFLICTING_VALUES,
        Reason.IMAGE_ON_PAGE,
    ]
    assert response.model_dump(mode="json")["fields"][2] == {
        "field": "referring_doctor",
        "status": "absent",
        "level": None,
        "reasons": [],
        "value": None,
        "span": None,
        "page": None,
    }
    assert isinstance(response.id, UUID) and referral().kind == response.kind


def test_the_response_carries_the_outcomes_kind_status_failure_and_pages() -> None:
    stored = StoredExtraction(STORED_ID, CREATED, CREATED)
    failed = dataclasses.replace(
        outcome(REVIEWED, failure=Reason.TRUNCATED),
        kind="vaccination_record",
        pages=3,
        image_pages=(2, 3),
    )
    body = ExtractionResponse.of(failed, stored).model_dump(mode="json")

    assert (body["kind"], body["status"], body["failure"], body["pages"], body["image_pages"]) == (
        "vaccination_record",
        "needs_review",
        "truncated",
        3,
        [2, 3],
    )
    assert ExtractionResponse.of(outcome(ACCEPTED), stored).status is DocumentStatus.COMPLETE

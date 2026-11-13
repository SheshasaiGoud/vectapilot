"""M6 PR 3: the route, the store and the purge against real Postgres. Integration only.

Throwaway businesses (`ext-<letter>-<hex>`) are registered by the owner and given an extraction
folder (the committed templates and a copy of Paws & Co's file under their id), so the app loads
them at startup as it loads the fictional businesses; they are removed afterwards. The app runs as
the app role, the purge as the owner (`VECTAPILOT_MIGRATION_DATABASE_URL`), the bootstrap superuser
locally and in CI (ADR 0009, "Limit"), where RLS never applies.

- **Stored as returned, the hash, never the file** (E10, D6 (a)): the rows equal the 201 body (its
  expiry is the database's, from the business's own retention), the file's SHA-256 matches, and no
  row holds the document's other text.
- **A second upload is a second extraction** [proposal].
- **E3's failures are stored all-review**; a hostile value (NFKC makes it longer than its field, or
  it holds NUL) is stored in review, never a 503; a database error (a refused statement, a
  connection that fails) is a 503 and stores nothing, with no value in a log (E9).
- **No quote, no value** (E2, decided 2026-11-13): a value E2 did not verify (an invented quote,
  a real quote with another value, a value cut short) is stored and returned in review with its
  reasons and without value, level, page or span; a verified value is still stored.
- **Expiry (E10):** an expired extraction and its fields are invisible to the app role and deleted
  by the owner's purge; purging A leaves every other row of every business table the same (B's
  expired rows included), and the same check turns red when the purge loses its business clause.
"""

import asyncio
import hashlib
import io
import logging
import secrets
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.canary.catalog import business_tables
from tests.conftest import integration
from tests.extraction.fakes import CANARY, vaccination
from tests.extraction.helpers import read_json, write_json
from tests.extraction.served import Served, extraction_folder, served
from tests.extraction.standins import (
    VACCINATION_ANSWER,
    VACCINATION_LINES,
    accept_everything,
    pdf,
    raising,
    reply,
    with_changes,
)
from tests.tools.sandbox_db import app_url, fresh_engine, owner_url
from tests.tools.test_sandbox_reset import PLANT_M2, VECTOR
from vectapilot_api.config import Settings
from vectapilot_api.db import Database, business_transaction, create_api_engine
from vectapilot_api.extraction import cli, purge
from vectapilot_api.extraction import store as extraction_store
from vectapilot_api.extraction.files import BUSINESSES
from vectapilot_api.extraction.purge import PurgeRefused, purge_expired
from vectapilot_api.extraction.router import NOT_STORED
from vectapilot_api.extraction.service import Route
from vectapilot_api.extraction.signals import EXTRACTION_RULES_VERSION
from vectapilot_api.extraction.store import PostgresExtractionStore
from vectapilot_api.gateway.errors import GatewayError, Transient
from vectapilot_api.gateway.providers.fake import INVALID_JSON_TEXT, FakeProvider, FakeReply
from vectapilot_api.log import JsonFormatter

pytestmark = [integration, pytest.mark.anyio]

KIND: Final = "vaccination_record"
SOURCE: Final = "paws-and-co"
"""The committed business file the throwaway businesses copy (a `veterinary` business)."""
RETENTION_DAYS: Final = 7
"""A retention other than the committed files' 30, so a hard-coded or default one is caught."""
DOCUMENT: Final = pdf(list(VACCINATION_LINES))
"""A made-up vaccination record; its notes line (no field) holds the canary."""
EXTRACTION_TABLES: Final = frozenset({"extractions", "extraction_fields"})
FORMATTER: Final = JsonFormatter()

EXTRACTION_ROW: Final = text(
    "SELECT kind, template_version, prompt_version, rules_version, status, failure_reason,"
    " file_sha256, pages, image_pages, trace_id, retention_days, expires_at - created_at AS kept,"
    " expires_at FROM extractions WHERE business_id = :b AND id = :id"
)
FIELD_ROWS: Final = text(
    "SELECT field, status, level, reasons, value #>> '{}' AS value, span, page"
    " FROM extraction_fields WHERE business_id = :b AND extraction_id = :id"
)
RETURNED: Final = ("status", "level", "reasons", "value", "span", "page")
"""A returned field's keys in `FIELD_ROWS`'s order after `field`."""
IDS: Final = text("SELECT id FROM extractions WHERE business_id = :b")
COUNT_EXTRACTIONS: Final = text("SELECT count(*) FROM extractions WHERE business_id = :b")
COUNT_FIELDS: Final = text("SELECT count(*) FROM extraction_fields WHERE business_id = :b")
"""Each read names its business: the owner skips RLS, so a bare count would see every one."""
FIELD_PARENTS: Final = text(
    "SELECT DISTINCT extraction_id FROM extraction_fields WHERE business_id = :b"
)
EVERY_ROW: Final = text(
    "SELECT row_to_json(e)::text FROM extractions e WHERE business_id = :b"
    " UNION ALL SELECT row_to_json(f)::text FROM extraction_fields f WHERE business_id = :b"
)
AGE: Final = text(
    "UPDATE extractions SET created_at = created_at - make_interval(days => :days)"
    " WHERE business_id = :b AND id = :id"
)
"""The owner ages an extraction (the app role cannot update): `expires_at` follows `created_at`."""
PLANT_EXPIRED: Final = text(
    "INSERT INTO extractions (business_id, kind, template_version, prompt_version,"
    " rules_version, status, file_sha256, pages, image_pages, trace_id, retention_days,"
    " created_at) VALUES (:b, :kind, 1, 'extract-fields.1', 'extraction-rules.1',"
    " 'needs_review', repeat('c', 64), 1, '{}', repeat('d', 32), 30,"
    " now() - make_interval(days => :age)) RETURNING id"
)
PLANT_FIELD: Final = text(
    "INSERT INTO extraction_fields (business_id, extraction_id, field, status, level, reasons,"
    " value, span, page) VALUES (:b, :id, 'pet_name', 'review', 'medium', ARRAY['unsure'],"
    " to_jsonb(CAST(:b AS text)), NULL, 1)"
)
REMOVE: Final = (
    "DELETE FROM tool_audit WHERE business_id = :b",
    "DELETE FROM llm_calls WHERE business_id = :b",
    "DELETE FROM extractions WHERE business_id = :b",  # fields go with their extraction
    "DELETE FROM documents WHERE business_id = :b",  # chunks go with their document
    "DELETE FROM businesses WHERE business_id = :b",
)
FINGERPRINT: Final = (
    "SELECT count(*), coalesce(md5(string_agg(row_to_json(t)::text, chr(10)"
    " ORDER BY row_to_json(t)::text)), '') FROM {table} AS t WHERE {where}"
)


@pytest.fixture
async def owner() -> AsyncIterator[AsyncEngine]:
    engine = fresh_engine(owner_url())
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_role() -> AsyncIterator[AsyncEngine]:
    engine = fresh_engine(app_url())
    yield engine
    await engine.dispose()


def new_business(letter: str) -> str:
    return f"ext-{letter}-{secrets.token_hex(4)}"


async def register(owner: AsyncEngine, business_id: str) -> None:
    async with business_transaction(owner, business_id) as connection:
        await connection.execute(
            text("INSERT INTO businesses (business_id) VALUES (:b)"), {"b": business_id}
        )


async def uproot(owner: AsyncEngine, business_id: str) -> None:
    async with business_transaction(owner, business_id) as connection:
        for statement in REMOVE:
            await connection.execute(text(statement), {"b": business_id})


@asynccontextmanager
async def businesses(
    owner: AsyncEngine, tmp_path: Path, *letters: str, retention_days: int | None = None
) -> AsyncIterator[tuple[Settings, list[str]]]:
    """Fresh registered businesses with Paws & Co's extraction settings (and `retention_days`, if
    given), and the app's Settings (the database URLs from the environment, as in CI); removed
    afterwards."""
    ids = [new_business(letter) for letter in letters]
    folder, corpus = extraction_folder(tmp_path, dict.fromkeys(ids, SOURCE))
    if retention_days is not None:
        for business_id in ids:
            path = folder / BUSINESSES / f"{business_id}.json"
            write_json(path, read_json(path) | {"retention_days": retention_days})
    try:
        for business_id in ids:
            await register(owner, business_id)
        yield Settings(extraction_config_dir=folder, corpus_dir=corpus), ids
    finally:
        for business_id in ids:
            await uproot(owner, business_id)


def right_answer() -> FakeReply:
    return reply(vaccination(), dict(VACCINATION_ANSWER))


@contextmanager
def app(
    settings: Settings,
    *script: FakeReply | GatewayError,
    route: Route | None = accept_everything,
    caplog: pytest.LogCaptureFixture | None = None,
) -> Iterator[Served]:
    with served(settings, FakeProvider(list(script)), route=route, caplog=caplog) as running:
        yield running


async def rows(engine: AsyncEngine, business_id: str, statement: Any, **params: Any) -> list[Any]:
    async with business_transaction(engine, business_id) as connection:
        return list((await connection.execute(statement, {"b": business_id, **params})).all())


async def run(engine: AsyncEngine, business_id: str, statement: Any, **params: Any) -> None:
    async with business_transaction(engine, business_id) as connection:
        await connection.execute(statement, {"b": business_id, **params})


# ── Stored as returned ────────────────────────────────────────────────────────────────────────


async def test_a_stored_extraction_is_its_response_with_the_files_hash_and_no_other_text(
    owner: AsyncEngine, app_role: AsyncEngine, tmp_path: Path
) -> None:
    async with businesses(owner, tmp_path, "a", retention_days=RETENTION_DAYS) as (settings, [b]):
        with app(settings, right_answer()) as running:
            response = running.upload(b, DOCUMENT, kind=KIND)
        assert response.status_code == 201, response.text
        body = response.json()
        stored_id = UUID(body["id"])
        [row] = await rows(app_role, b, EXTRACTION_ROW, id=stored_id)
        fields = await rows(app_role, b, FIELD_ROWS, id=stored_id)
        everything = "\n".join(r[0] for r in await rows(owner, b, EVERY_ROW))
        columns = await rows(
            owner,
            b,
            text(
                "SELECT data_type FROM information_schema.columns"
                " WHERE table_name IN ('extractions', 'extraction_fields')"
            ),
        )

    assert tuple(row) == (
        KIND,
        1,
        "extract-fields.1",
        EXTRACTION_RULES_VERSION,
        body["status"],
        None,
        hashlib.sha256(DOCUMENT).hexdigest(),
        1,
        [],
        response.headers["X-Trace-Id"],
        RETENTION_DAYS,
        timedelta(days=RETENTION_DAYS),
        datetime.fromisoformat(body["expires_at"]),  # the body's expiry is the database's
    )
    assert {f.field: tuple(f)[1:] for f in fields} == {
        f["field"]: (f["status"], f["level"], f["reasons"], f["value"], f["span"], f["page"])
        for f in body["fields"]
    }
    assert any(f["status"] == "accepted" and f["value"] for f in body["fields"])  # values stored
    assert CANARY not in everything  # the document's other lines are never stored
    assert "bytea" not in {c.data_type for c in columns}  # nowhere to keep the file


async def test_a_second_upload_of_the_same_file_is_a_second_extraction(
    owner: AsyncEngine, app_role: AsyncEngine, tmp_path: Path
) -> None:
    async with businesses(owner, tmp_path, "a") as (settings, [b]):
        with app(settings, right_answer(), right_answer()) as running:
            first = running.upload(b, DOCUMENT, kind=KIND)
            second = running.upload(b, DOCUMENT, kind=KIND)
        stored = await rows(
            app_role, b, text("SELECT id, file_sha256 FROM extractions WHERE business_id = :b")
        )

    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["id"] != second.json()["id"]
    assert first.headers["X-Trace-Id"] != second.headers["X-Trace-Id"]
    assert sorted(str(r.id) for r in stored) == sorted([first.json()["id"], second.json()["id"]])
    assert {r.file_sha256 for r in stored} == {hashlib.sha256(DOCUMENT).hexdigest()}


# ── E3: failures are stored all-review; a database error stores nothing ───────────────────────


@pytest.mark.parametrize(
    ("script", "route", "failure"),
    [
        ((Transient(status_code=529),), accept_everything, "model_unavailable"),
        (
            (FakeReply(text=INVALID_JSON_TEXT), FakeReply(text=f'{{"x": "{CANARY}"}}')),
            accept_everything,
            "invalid_output",
        ),
        (
            (FakeReply(text=f'{{"pet_name": "{CANARY}', stop_reason="max_tokens"),),
            accept_everything,
            "truncated",
        ),
        ((right_answer(),), raising, "internal_error"),
    ],
    ids=["outage", "invalid_twice", "truncated", "routing_raises"],
)
async def test_e3s_failures_are_stored_with_every_field_in_review(
    owner: AsyncEngine,
    app_role: AsyncEngine,
    tmp_path: Path,
    script: tuple[FakeReply | GatewayError, ...],
    route: Route,
    failure: str,
) -> None:
    async with businesses(owner, tmp_path, "a") as (settings, [b]):
        with app(settings, *script, route=route) as running:
            response = running.upload(b, DOCUMENT, kind=KIND)
        assert response.status_code == 201, response.text
        stored_id = UUID(response.json()["id"])
        [row] = await rows(app_role, b, EXTRACTION_ROW, id=stored_id)
        fields = await rows(app_role, b, FIELD_ROWS, id=stored_id)

    assert (row.status, row.failure_reason) == ("needs_review", failure)
    assert (response.json()["status"], response.json()["failure"]) == ("needs_review", failure)
    assert len(fields) == len(vaccination().template.fields)
    assert all(
        (f.status, f.level, f.reasons, f.value, f.span, f.page)
        == ("review", None, [failure], None, None, None)
        for f in fields
    )


@pytest.mark.parametrize(
    ("proposed", "stored"),
    [
        pytest.param(("\ufdfa" * 30, "nowhere in the document"), None, id="NFKC makes 540"),
        pytest.param(("Bis\x00cuit", "Pet name: Bis\x00cuit"), "Biscuit", id="NUL"),
    ],
)
async def test_a_hostile_value_is_stored_never_a_503(
    owner: AsyncEngine,
    app_role: AsyncEngine,
    tmp_path: Path,
    proposed: tuple[str, str],
    stored: str | None,
) -> None:
    # The real store and migration 0007's checks: before the review fix both were a 503 and the
    # upload was lost (CHECK 23514 for the length, 22021 for NUL).
    answer = with_changes(VACCINATION_ANSWER, pet_name=proposed)
    async with businesses(owner, tmp_path, "a") as (settings, [b]):
        with app(settings, reply(vaccination(), answer)) as running:
            response = running.upload(b, DOCUMENT, kind=KIND)
        assert response.status_code == 201, response.text
        fields = await rows(app_role, b, FIELD_ROWS, id=UUID(response.json()["id"]))

    pet = next(f for f in fields if f.field == "pet_name")
    assert (pet.value, pet.span, pet.page) == (
        (None, None, None) if stored is None else (stored, "Pet name: Biscuit", 1)
    )
    if stored is None:  # withheld: in review, no level
        assert (pet.status, pet.level) == ("review", None) and "too_long" in pet.reasons
    assert len(fields) == len(vaccination().template.fields)


@pytest.mark.parametrize(
    ("field", "proposed", "kept"),
    [
        pytest.param("pet_name", ("Rexwell", "Pet name: Rexwell"), None, id="an invented quote"),
        pytest.param(
            "owner_name",
            ("Priya Menon", "Owner name: Ravi Menon"),
            None,
            id="a real quote with another value",
        ),
        pytest.param("owner_name", ("Ravi", "Owner name: Ravi"), None, id="a value cut short"),
        pytest.param(
            "owner_name",
            ("Ravi Menon", "Owner name: Ravi Menon", True),  # unsure: in review, still verified
            ("low", "Ravi Menon", "Owner name: Ravi Menon", 1),
            id="a verified value is still stored",
        ),
    ],
)
async def test_only_a_value_e2_verified_is_stored_and_returned(
    owner: AsyncEngine,
    app_role: AsyncEngine,
    tmp_path: Path,
    field: str,
    proposed: tuple[str, str] | tuple[str, str, bool],
    kept: tuple[str, str, str, int] | None,
) -> None:
    # Decided 2026-11-13 (shesha, M6 PR 3 review): the real route, store and 0007's checks.
    answer = with_changes(VACCINATION_ANSWER, **{field: proposed})
    async with businesses(owner, tmp_path, "a") as (settings, [b]):
        with app(settings, reply(vaccination(), answer)) as running:
            response = running.upload(b, DOCUMENT, kind=KIND)
        assert response.status_code == 201, response.text
        fields = await rows(app_role, b, FIELD_ROWS, id=UUID(response.json()["id"]))
        everything = "\n".join(r[0] for r in await rows(owner, b, EVERY_ROW))

    stored = next(f for f in fields if f.field == field)
    returned = next(f for f in response.json()["fields"] if f["field"] == field)
    assert tuple(stored)[1:] == tuple(returned[key] for key in RETURNED)  # stored as returned
    # The stand-in accepted it; the invariant check moved it to review: status and reasons stay.
    assert (stored.status, stored.reasons) == ("review", ["routing_invariant"])
    assert (stored.level, stored.value, stored.span, stored.page) == (
        kept or (None, None, None, None)
    )
    assert (proposed[0] in everything) == (kept is not None)  # an unverified guess is nowhere


async def test_a_refused_statement_is_a_503_that_stores_nothing_and_logs_no_value(
    owner: AsyncEngine,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The service never sends a row 0007 refuses, so the refusal is made in the store: every field
    # row gets a reason 0007 does not know, after the extraction row went in. The value holds the
    # canary, and the database's error text may hold the row.
    cast = "CAST(:reasons AS text[])"
    assert cast in str(extraction_store.INSERT_FIELD), "INSERT_FIELD changed: update this test"
    refused = str(extraction_store.INSERT_FIELD).replace(cast, f"{cast} || ARRAY['made_up']")
    monkeypatch.setattr(extraction_store, "INSERT_FIELD", text(refused))
    lines = [line.replace("Biscuit", f"Biscuit {CANARY}") for line in VACCINATION_LINES]
    answer = with_changes(
        VACCINATION_ANSWER, pet_name=(f"Biscuit {CANARY}", f"Pet name: Biscuit {CANARY}")
    )
    caplog.set_level(logging.INFO)
    async with businesses(owner, tmp_path, "a") as (settings, [b]):
        with app(settings, reply(vaccination(), answer), caplog=caplog) as running:
            response = running.upload(b, pdf(lines), kind=KIND)
        stored = await rows(owner, b, EVERY_ROW)

    assert (response.status_code, response.json()["error"]) == (503, "database_unavailable")
    assert stored == []  # the extraction row rolled back with its refused fields
    [record] = [r for r in caplog.records if r.getMessage() == NOT_STORED]
    assert getattr(record, NOT_STORED)["error"] == "IntegrityError"
    assert getattr(record, NOT_STORED)["trace_id"] == response.headers["X-Trace-Id"]
    logged = "\n".join(FORMATTER.format(r) for r in caplog.records)
    assert CANARY not in logged and CANARY not in response.text


async def test_a_store_whose_connection_fails_is_a_503_that_stores_nothing(
    owner: AsyncEngine, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Nothing listens on port 1 of the loopback: the connection is refused at once.
    unreachable = create_api_engine("postgresql+asyncpg://vectapilot_app:x@127.0.0.1:1/vectapilot")
    caplog.set_level(logging.INFO)
    try:
        async with businesses(owner, tmp_path, "a") as (settings, [b]):
            with app(settings, right_answer(), caplog=caplog) as running:
                running.app.state.extraction_store = PostgresExtractionStore(Database(unreachable))
                response = running.upload(b, DOCUMENT, kind=KIND)
            stored = await rows(owner, b, EVERY_ROW)
    finally:
        await unreachable.dispose()

    assert (response.status_code, response.json()["error"]) == (503, "database_unavailable")
    assert stored == []
    [record] = [r for r in caplog.records if r.getMessage() == NOT_STORED]
    assert getattr(record, NOT_STORED)["error"] in {"ConnectionRefusedError", "OperationalError"}


# ── E10: hidden at expiry, purged by the owner ────────────────────────────────────────────────


async def test_an_expired_extraction_is_hidden_from_the_app_role_and_purged_by_the_owner(
    owner: AsyncEngine, app_role: AsyncEngine, tmp_path: Path
) -> None:
    async with businesses(owner, tmp_path, "a") as (settings, [b]):
        with app(settings, right_answer(), right_answer()) as running:
            old = UUID(running.upload(b, DOCUMENT, kind=KIND).json()["id"])
            new = UUID(running.upload(b, DOCUMENT, kind=KIND).json()["id"])
        await run(owner, b, AGE, id=old, days=31)  # retention 30: expired a day ago

        app_ids = {r.id for r in await rows(app_role, b, IDS)}
        app_parents = {r.extraction_id for r in await rows(app_role, b, FIELD_PARENTS)}
        owner_ids = {r.id for r in await rows(owner, b, IDS)}
        purged = await cli.purge_with_owner(owner_url(), b)
        after = {r.id for r in await rows(owner, b, IDS)}
        parents_after = {r.extraction_id for r in await rows(owner, b, FIELD_PARENTS)}
        again = await cli.purge_with_owner(owner_url(), b)

    assert (app_ids, app_parents) == ({new}, {new})  # hidden from the app role, fields too
    assert owner_ids == {old, new}  # ...but still there until the purge
    assert (purged, after, parents_after, again) == (1, {new}, {new}, 0)


@asynccontextmanager
async def two_with_expired_rows(
    owner: AsyncEngine, tmp_path: Path
) -> AsyncIterator[tuple[str, str]]:
    """Businesses a and b, each with an expired and an unexpired extraction (one field each)
    and M2-M5 rows (a document and its chunk, an `llm_calls` row, a `tool_audit` row)."""
    async with businesses(owner, tmp_path, "a", "b") as (_, [a, b]):
        for business_id in (a, b):
            params = {"b": business_id, "kind": KIND, "vector": VECTOR}
            async with business_transaction(owner, business_id) as connection:
                for statement in PLANT_M2:
                    await connection.execute(statement, params)
                for age in (31, 1):
                    extraction = await connection.scalar(PLANT_EXPIRED, params | {"age": age})
                    await connection.execute(PLANT_FIELD, params | {"id": extraction})
        yield a, b


async def fingerprint(
    connection: AsyncConnection, *, except_extractions_of: str
) -> Mapping[str, tuple[int, str]]:
    """Every business table (from the catalog), as a row count and an MD5 of its rows' JSON:
    every row of every business except `except_extractions_of`'s extraction rows. Read on the
    owner's connection, which skips RLS (and the expiry policy), so it sees every row."""
    prints: dict[str, tuple[int, str]] = {}
    for table in await business_tables(connection):
        where = "business_id IS DISTINCT FROM :skip" if table in EXTRACTION_TABLES else "true"
        row = (
            await connection.execute(
                text(FINGERPRINT.format(table=table, where=where)),
                {"skip": except_extractions_of},
            )
        ).one()
        prints[table] = (int(row[0]), str(row[1]))
    return prints


async def test_purging_a_leaves_every_other_row_of_every_business_table_unchanged(
    owner: AsyncEngine, tmp_path: Path
) -> None:
    async with two_with_expired_rows(owner, tmp_path) as (a, b):
        async with owner.connect() as connection:
            before = await fingerprint(connection, except_extractions_of=a)
        purged = await cli.purge_with_owner(owner_url(), a)  # the command's own path, committed
        async with owner.connect() as connection:
            after = await fingerprint(connection, except_extractions_of=a)
        a_left = await rows(owner, a, COUNT_FIELDS)
        b_left = await rows(owner, b, COUNT_EXTRACTIONS)

    assert set(before) >= {*EXTRACTION_TABLES, "documents", "chunks", "llm_calls", "tool_audit"}
    assert before["extractions"][0] > 0 and before["documents"][0] > 0
    assert after == before  # b's expired row too, and every M2-M5 table
    assert purged == 1  # the positive control: a's expired extraction went...
    assert a_left[0][0] == 1  # ...with its field; a's unexpired one stays
    assert b_left[0][0] == 2


async def test_red_proof_a_purge_without_its_business_clause_is_caught(
    owner: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clause = "business_id = :business_id AND "
    assert clause in str(purge.PURGE), "the purge's business clause changed: update this proof"
    monkeypatch.setattr(purge, "PURGE", text(str(purge.PURGE).replace(clause, "")))
    async with two_with_expired_rows(owner, tmp_path) as (a, _), owner.connect() as connection:
        try:
            before = await fingerprint(connection, except_extractions_of=a)
            await connection.execute(
                text("SELECT set_config('app.business_id', :b, true)"), {"b": a}
            )
            await purge_expired(connection, a)  # inside this transaction, rolled back
            after = await fingerprint(connection, except_extractions_of=a)
        finally:
            await connection.rollback()

    assert after["extractions"] != before["extractions"]  # b's expired extraction went too
    assert {t: p for t, p in after.items() if t not in EXTRACTION_TABLES} == {
        t: p for t, p in before.items() if t not in EXTRACTION_TABLES
    }


async def test_the_purge_refuses_a_connection_for_another_business_or_none(
    owner: AsyncEngine, tmp_path: Path
) -> None:
    async with two_with_expired_rows(owner, tmp_path) as (a, b):
        with pytest.raises(PurgeRefused):
            async with business_transaction(owner, b) as connection:
                await purge_expired(connection, a)
        with pytest.raises(PurgeRefused):
            async with owner.begin() as connection:
                await purge_expired(connection, a)
        left = [await rows(owner, x, COUNT_EXTRACTIONS) for x in (a, b)]

    assert [count[0][0] for count in left] == [2, 2]


def test_the_command_purges_a_business_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sync on purpose: the command runs its own event loop (asyncio.run). The environment is
    # required (no default), so the test names it.
    monkeypatch.setenv("VECTAPILOT_ENVIRONMENT", "test")
    business = new_business("cli")

    async def plant() -> None:
        engine = fresh_engine(owner_url())
        try:
            await register(engine, business)
            async with business_transaction(engine, business) as connection:
                for age in (31, 40, 1):
                    params = {"b": business, "kind": KIND, "age": age}
                    await connection.scalar(PLANT_EXPIRED, params)
        finally:
            await engine.dispose()

    async def count_and_uproot() -> int:
        engine = fresh_engine(owner_url())
        try:
            return len(await rows(engine, business, IDS))
        finally:
            await uproot(engine, business)
            await engine.dispose()

    asyncio.run(plant())
    out, err = io.StringIO(), io.StringIO()
    try:
        code = cli.main(["purge", "--business", business], out=out, err=err)
    finally:
        left = asyncio.run(count_and_uproot())

    assert (code, err.getvalue(), left) == (0, "", 1)
    assert out.getvalue().startswith(f"extraction purge: {business}: 2 expired extraction(s)")

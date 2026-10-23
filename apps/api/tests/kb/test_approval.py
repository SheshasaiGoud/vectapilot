"""One approved version per source (M2 PR 5b, ADR 0010), without a database.

Two halves:
- the rules through `POST /v1/kb/documents`, with the in-memory store (`fakes.py`, the same
  contract as Postgres with migration 0004);
- `PostgresKbStore.save_document` against a scripted connection: the order of its statements in
  one transaction, the rollback of the retire, and the typed `ApprovalConflict`.

The same rules against real Postgres (exactly one approved version afterwards, the rollback when a
write fails, the concurrent approval, the index itself): test_postgres_store.py. Across businesses
(under RLS): the canary write path `approve_a_new_version` (tests/canary/write_paths.py).
"""

import importlib
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any, Final, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import IntegrityError

from tests.kb.test_routes import CANARY, MARKDOWN, PARAMS, A, B, Kb, assert_error, build_kb
from tests.test_migrations import migration_0004
from vectapilot_api.config import Settings
from vectapilot_api.db import Database
from vectapilot_api.ingestion.types import SourceType
from vectapilot_api.kb import store as kb_store
from vectapilot_api.kb.router import ERRORS
from vectapilot_api.kb.store import (
    ONE_APPROVED_INDEX,
    UNIQUE_VIOLATION,
    PostgresKbStore,
    is_one_approved_violation,
)
from vectapilot_api.kb.types import (
    ApprovalConflict,
    ContentKind,
    DocumentStatus,
    LanguageConflict,
    NewChunk,
    NewDocument,
    StatusConflict,
)

pytestmark = pytest.mark.anyio

SOURCE: Final = PARAMS["source_id"]
SECRET_SOURCE: Final = f"src-{CANARY}"
"""A source id that must never come back in an error body."""
LANGUAGE: Final = PARAMS["language"]
CANARY_LANGUAGE: Final = "hi-Zq91c2"
"""A valid tag (a made-up region subtag) that must never come back in an error body."""


@pytest.fixture
def kb(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[Kb]:
    app, store, embedder, clock = build_kb(settings, monkeypatch)
    with TestClient(app) as client:
        yield Kb(client, store, embedder, clock)


def upload(
    kb: Kb,
    version: str,
    status: str,
    *,
    body: bytes = MARKDOWN,
    source: str = SOURCE,
    business: str = A,
    language: str = LANGUAGE,
) -> Any:
    params = {"source_id": source, "version": version, "status": status, "language": language}
    return kb.post(body, params=params, business=business)


# ── Through the route: approving retires, nothing else does ─────────────────────────────────


def test_approving_a_new_version_retires_the_approved_one(kb: Kb) -> None:
    first = upload(kb, "1", "approved")
    second = upload(kb, "2", "approved")

    assert (first.status_code, second.status_code) == (201, 201), second.text
    assert first.json()["retired_version"] is None  # the source's first approved version
    assert second.json()["retired_version"] == "1"
    assert kb.store.statuses(A, SOURCE) == {"1": "retired", "2": "approved"}


def test_each_approval_retires_the_current_one_and_only_it(kb: Kb) -> None:
    responses = [upload(kb, version, "approved") for version in ("1", "2", "3")]

    assert [r.json()["retired_version"] for r in responses] == [None, "1", "2"]
    assert kb.store.statuses(A, SOURCE) == {"1": "retired", "2": "retired", "3": "approved"}


def test_approving_retires_the_approved_version_and_leaves_a_pending_draft_alone(kb: Kb) -> None:
    steps = (("1", "approved"), ("2", "approved"), ("3", "draft"), ("4", "approved"))
    responses = [upload(kb, version, status) for version, status in steps]

    assert [r.status_code for r in responses] == [201] * 4
    assert responses[-1].json()["retired_version"] == "2"
    assert kb.store.statuses(A, SOURCE) == {
        "1": "retired",
        "2": "retired",
        "3": "draft",
        "4": "approved",
    }


@pytest.mark.parametrize("status", ["draft", "retired"])
def test_a_draft_or_retired_upload_retires_nothing(kb: Kb, status: str) -> None:
    upload(kb, "1", "approved")
    response = upload(kb, "2", status)

    assert response.status_code == 201 and response.json()["retired_version"] is None
    assert kb.store.statuses(A, SOURCE) == {"1": "approved", "2": status}


def test_approving_retires_only_that_source_of_that_business(kb: Kb) -> None:
    upload(kb, "1", "approved")
    upload(kb, "1", "approved", source="prices")
    upload(kb, "1", "approved", business=B)  # the same source id, another business

    upload(kb, "2", "approved")

    assert kb.store.statuses(A, SOURCE) == {"1": "retired", "2": "approved"}
    assert kb.store.statuses(A, "prices") == {"1": "approved"}
    assert kb.store.statuses(B, SOURCE) == {"1": "approved"}


# ── Re-uploading a stored version: same bytes and status = no-op, else 409 ──────────────────


@pytest.mark.parametrize("status", ["draft", "approved", "retired"])
def test_the_same_bytes_and_status_again_are_a_no_op(kb: Kb, status: str) -> None:
    first = upload(kb, "1", status)
    again = upload(kb, "1", status)

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["retired_version"] is None and again.json()["created"] is False
    assert kb.store.statuses(A, SOURCE) == {"1": status}
    assert len(kb.embedder.batches) == 1


@pytest.mark.parametrize("race", [False, True], ids=["checked first", "found on write"])
def test_re_approving_a_retired_version_is_a_status_conflict(kb: Kb, race: bool) -> None:
    upload(kb, "1", "approved", source=SECRET_SOURCE)
    upload(kb, "2", "approved", source=SECRET_SOURCE)
    kb.store.hide_from_find = race

    response = upload(kb, "1", "approved", source=SECRET_SOURCE)  # a rollback attempt

    assert_error(response, 409, "status_conflict")
    assert kb.store.statuses(A, SECRET_SOURCE) == {"1": "retired", "2": "approved"}


@pytest.mark.parametrize(
    ("stored", "requested"),
    [("draft", "approved"), ("approved", "draft"), ("approved", "retired"), ("retired", "draft")],
)
def test_the_same_bytes_with_another_status_are_a_status_conflict(
    kb: Kb, stored: str, requested: str
) -> None:
    upload(kb, "1", stored)

    assert_error(upload(kb, "1", requested), 409, "status_conflict")
    assert kb.store.statuses(A, SOURCE) == {"1": stored}


def test_other_bytes_stay_a_version_conflict_whatever_the_status(kb: Kb) -> None:
    upload(kb, "1", "retired")

    response = upload(kb, "1", "approved", body=MARKDOWN + b"\nchanged\n")

    assert_error(response, 409, "version_conflict")
    assert kb.store.statuses(A, SOURCE) == {"1": "retired"}


@pytest.mark.parametrize("language", [LANGUAGE, "en-IN", "hi"])
def test_the_same_bytes_status_and_language_again_are_a_no_op(kb: Kb, language: str) -> None:
    first = upload(kb, "1", "approved", language=language)
    again = upload(kb, "1", "approved", language=language)

    assert (first.status_code, again.status_code) == (201, 200), again.text
    assert again.json()["created"] is False
    assert again.json()["document_id"] == first.json()["document_id"]
    assert len(kb.embedder.batches) == 1


@pytest.mark.parametrize("race", [False, True], ids=["checked first", "found on write"])
@pytest.mark.parametrize(
    "language",
    ["hi", "en-IN", "en-in", CANARY_LANGUAGE],
    ids=["another language", "same config", "case only", "never echoed"],
)
def test_the_same_bytes_with_another_language_are_a_language_conflict(
    kb: Kb, race: bool, language: str
) -> None:
    """Compared as stored, never through the map: `en-IN` (also `english`) and a spelling that
    differs in case only are conflicts too. Nothing is stored or changed."""
    stored_language = "en-IN" if language == "en-in" else LANGUAGE
    upload(kb, "1", "approved", language=stored_language)
    documents, chunks = dict(kb.store.documents), set(kb.store.chunks)
    batches = len(kb.embedder.batches)
    kb.store.hide_from_find = race

    response = upload(kb, "1", "approved", language=language)

    assert_error(response, 409, "language_conflict")
    assert response.json()["detail"] == ERRORS[LanguageConflict][2]
    assert "Zq91c2" not in response.text
    assert (kb.store.documents, set(kb.store.chunks)) == (documents, chunks)
    [(_, document)] = kb.store.documents.values()
    assert document.language == stored_language
    # Checked first: refused before loading, nothing embedded. Found on write (a concurrent
    # upload stored it after the check): embedded once more, then refused, nothing written.
    assert len(kb.embedder.batches) == batches + race


def test_another_status_is_reported_before_another_language(kb: Kb) -> None:
    upload(kb, "1", "draft")

    assert_error(upload(kb, "1", "approved", language="hi"), 409, "status_conflict")
    assert kb.store.statuses(A, SOURCE) == {"1": "draft"}


def test_other_bytes_are_reported_before_another_language(kb: Kb) -> None:
    upload(kb, "1", "draft")

    response = upload(kb, "1", "draft", body=MARKDOWN + b"\nchanged\n", language="hi")

    assert_error(response, 409, "version_conflict")


def test_a_concurrent_approval_is_a_typed_409_that_echoes_nothing(kb: Kb) -> None:
    upload(kb, "1", "approved", source=SECRET_SOURCE)
    kb.store.fail_on_save = ApprovalConflict()  # migration 0004's index refused the insert

    response = upload(kb, "2", "approved", source=SECRET_SOURCE)

    assert_error(response, 409, "approval_conflict")
    assert response.json()["detail"] == ERRORS[ApprovalConflict][2]
    assert kb.store.statuses(A, SECRET_SOURCE) == {"1": "approved"}


def test_the_conflicts_have_their_own_stable_codes() -> None:
    assert ERRORS[StatusConflict][:2] == (409, "status_conflict")
    assert ERRORS[LanguageConflict][:2] == (409, "language_conflict")
    assert ERRORS[ApprovalConflict][:2] == (409, "approval_conflict")


# ── PostgresKbStore.save_document against a scripted connection ─────────────────────────────

# asyncpg ships no type hints: its error classes are read as `Any`.
_asyncpg_errors: Any = importlib.import_module("asyncpg.exceptions")
_adapted_integrity_error: Any = AsyncAdapt_asyncpg_dbapi.IntegrityError

CHECK_VIOLATION: Final = "23514"


def integrity_error(sqlstate: str, constraint: str) -> IntegrityError:
    """An `IntegrityError` shaped as SQLAlchemy 2.1's asyncpg dialect raises it: asyncpg's own
    error (with the constraint name) inside the adapter's (with the SQLSTATE)."""
    kind = {UNIQUE_VIOLATION: "UniqueViolationError", CHECK_VIOLATION: "CheckViolationError"}
    driver = getattr(_asyncpg_errors, kind[sqlstate]).new(
        {"C": sqlstate, "n": constraint, "M": f"violates {constraint}", "D": f"Key {CANARY}"}
    )
    adapted = _adapted_integrity_error(f"violates {constraint}", driver)
    return IntegrityError("INSERT ...", {"title": CANARY}, adapted)


class Result:
    def __init__(self, value: object = None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value

    def one(self) -> object:
        return self.value


class ScriptedConnection:
    """Records each statement by kind; answers or raises from `script` (kind -> value/error)."""

    KINDS: Final = {
        "UPDATE documents": "retire",
        "INSERT INTO documents": "insert_document",
        "INSERT INTO chunks": "insert_chunks",
        "SELECT id, content_sha256, status": "find",
    }

    def __init__(self, script: dict[str, object]) -> None:
        self.script = script
        self.statements: list[str] = []
        self.params: list[object] = []

    async def execute(self, statement: object, params: object = None) -> Result:
        kind = next(k for prefix, k in self.KINDS.items() if str(statement).startswith(prefix))
        self.statements.append(kind)
        self.params.append(params)
        answer = self.script.get(kind)
        if isinstance(answer, BaseException):
            raise answer
        return Result(answer)


class ScriptedDatabase:
    """`Database.business_transaction` over one scripted connection; records commit/rollback."""

    def __init__(self, connection: ScriptedConnection) -> None:
        self.connection = connection
        self.outcomes: list[str] = []

    @asynccontextmanager
    async def business_transaction(self, business_id: str) -> AsyncIterator[ScriptedConnection]:
        try:
            yield self.connection
        except BaseException:
            self.outcomes.append("rollback")
            raise
        self.outcomes.append("commit")


class FoundRow:
    def __init__(self, status: DocumentStatus) -> None:
        self.id = uuid.uuid4()
        self.content_sha256 = "b" * 64
        self.status = status
        self.language = "hi"  # not `new_document`'s "en": the stored row's value must come back


def new_document(status: DocumentStatus, version: str = "2") -> NewDocument:
    return NewDocument(
        source_id=SECRET_SOURCE,
        version=version,
        title="Title",
        source_type=SourceType.MARKDOWN,
        content_kind=ContentKind.FAQ,
        language="en",
        status=status,
        audience="public",
        content_sha256="a" * 64,
        review_by=None,
        metadata={},
    )


CHUNK: Final = NewChunk(chunk_index=0, content="text", page=None, metadata={}, embedding=(1.0,))


async def save(
    script: dict[str, object], status: DocumentStatus
) -> tuple[ScriptedConnection, ScriptedDatabase, Any]:
    connection = ScriptedConnection(script)
    database = ScriptedDatabase(connection)
    store = PostgresKbStore(cast(Database, database))
    stored = await store.save_document(A, new_document(status), [CHUNK], model="m", dimension=1)
    return connection, database, stored


async def test_an_approved_save_retires_first_then_inserts_in_one_transaction() -> None:
    document_id = uuid.uuid4()
    connection, database, stored = await save(
        {"retire": "1", "insert_document": document_id}, "approved"
    )

    assert connection.statements == ["retire", "insert_document", "insert_chunks"]
    assert database.outcomes == ["commit"]
    assert connection.params[0] == {"business_id": A, "source_id": SECRET_SOURCE, "version": "2"}
    assert (stored.created, stored.status, stored.retired_version) == (True, "approved", "1")
    assert stored.document_id == document_id


@pytest.mark.parametrize("status", ["draft", "retired"])
async def test_a_draft_or_retired_save_runs_no_retire(status: DocumentStatus) -> None:
    connection, database, stored = await save({"insert_document": uuid.uuid4()}, status)

    assert connection.statements == ["insert_document", "insert_chunks"]
    assert database.outcomes == ["commit"]
    assert stored.retired_version is None


async def test_a_version_stored_meanwhile_rolls_the_retire_back() -> None:
    row = FoundRow("draft")
    connection, database, stored = await save(
        {"retire": "1", "insert_document": None, "find": row}, "approved"
    )

    assert connection.statements == ["retire", "insert_document", "find"]
    assert database.outcomes == ["rollback"]  # the retire of version 1 is undone
    assert (stored.created, stored.status, stored.document_id) == (False, "draft", row.id)
    assert stored.language == "hi" and stored.retired_version is None


async def test_the_index_refusing_the_insert_is_an_approval_conflict_without_its_text() -> None:
    script: dict[str, object] = {
        "retire": None,
        "insert_document": integrity_error(UNIQUE_VIOLATION, ONE_APPROVED_INDEX),
    }
    with pytest.raises(ApprovalConflict) as raised:
        await save(script, "approved")

    # Raised outside the `except`: the IntegrityError (input and the database's `Key ...` detail)
    # is neither its cause nor its context, so no traceback or error tracker can reach it.
    assert raised.value.__context__ is None and raised.value.__cause__ is None
    assert CANARY not in str(raised.value) and CANARY not in repr(raised.value)


async def test_an_approval_conflict_after_a_retire_rolls_the_retire_back() -> None:
    """The retire matched version 1, then the index refused the insert. The violation must leave
    `business_transaction` as an exception: caught inside it, the transaction would end normally
    and commit the retire, leaving the source with no approved version at all."""
    connection = ScriptedConnection(
        {"retire": "1", "insert_document": integrity_error(UNIQUE_VIOLATION, ONE_APPROVED_INDEX)}
    )
    database = ScriptedDatabase(connection)
    store = PostgresKbStore(cast(Database, database))

    with pytest.raises(ApprovalConflict):
        await store.save_document(A, new_document("approved"), [CHUNK], model="m", dimension=1)
    assert database.outcomes == ["rollback"]  # version 1 stays approved
    assert connection.statements == ["retire", "insert_document"]  # no chunk insert


@pytest.mark.parametrize(
    "error",
    [
        integrity_error(UNIQUE_VIOLATION, "documents_business_id_source_id_version_key"),
        integrity_error(CHECK_VIOLATION, "chunks_embedding_dim_check"),
    ],
    ids=["another unique key", "a check"],
)
async def test_any_other_integrity_error_stays_a_database_error_and_rolls_back(
    error: IntegrityError,
) -> None:
    connection = ScriptedConnection({"retire": "1", "insert_document": uuid.uuid4()})
    connection.script["insert_chunks"] = error
    database = ScriptedDatabase(connection)
    store = PostgresKbStore(cast(Database, database))

    with pytest.raises(IntegrityError):
        await store.save_document(A, new_document("approved"), [CHUNK], model="m", dimension=1)
    assert database.outcomes == ["rollback"]  # the retire is undone with the rest


def test_the_violation_is_recognised_only_by_sqlstate_and_index_name() -> None:
    assert is_one_approved_violation(integrity_error(UNIQUE_VIOLATION, ONE_APPROVED_INDEX))
    assert not is_one_approved_violation(integrity_error(UNIQUE_VIOLATION, "another_index"))
    assert not is_one_approved_violation(integrity_error(CHECK_VIOLATION, ONE_APPROVED_INDEX))
    unknown = IntegrityError("INSERT ...", {}, Exception("some other driver"))
    assert not is_one_approved_violation(unknown)  # an unknown shape: not a 409


def test_the_store_names_migration_0004s_index() -> None:
    assert migration_0004().INDEX == ONE_APPROVED_INDEX


def test_the_retire_names_the_business_the_source_and_only_another_approved_version() -> None:
    """A local layer under the Postgres tests. The scripted connection matches statements by
    prefix and the canary write path runs under RLS, so neither notices a retire that lost a
    filter. Without `business_id`, RLS is the only wall left; without `source_id`, approving one
    source retires every other source of the business; without `status`, it retires pending
    drafts too (and `scalar_one_or_none` fails once a source has two other versions)."""
    head, where = str(kb_store._RETIRE_APPROVED).split(" WHERE ")
    predicates, returning = where.split(" RETURNING ")

    assert head == "UPDATE documents SET status = 'retired'"
    assert set(predicates.split(" AND ")) == {
        "business_id = :business_id",
        "source_id = :source_id",
        "status = 'approved'",
        "version <> :version",
    }
    assert returning == "version"

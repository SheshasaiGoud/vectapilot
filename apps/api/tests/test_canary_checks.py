"""The canary suite's checks and its CI gate, without a database: each one can turn red.

tests/canary/checks.py decides on catalog facts; these tests hand it broken facts (FORCE off, a
key without business_id, an owner role, a leaked canary) and expect a problem. The same checks run
on the real catalog, and break real tables on purpose, in tests/canary/test_leakage.py (CI).
Kept outside tests/canary/ on purpose: scripts/canary_gate.py must not count these as the suite.
"""

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Final, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from scripts.canary_gate import SuiteCounts, gate_problems, main, suite_counts
from tests.canary.checks import (
    ForeignKey,
    KeyIndex,
    Planted,
    Policy,
    Relation,
    RoleFacts,
    app_role_problems,
    canary_leaks,
    foreign_key_problems,
    key_problems,
    policy_problems,
    relation_problems,
)
from tests.canary.read_paths import (
    BAIT,
    NEEDED_OUTSIDE_READ_PATHS,
    NEEDS_HAND_WRITE,
    PENDING_HAND_WRITE,
    READ_PATHS,
    answer_through_the_api,
    pending_hand_write,
    pending_hand_writes,
    read_path_params,
    search_through_the_api,
)
from tests.conftest import make_settings
from tests.retrieval.standins import FakeDatabase, hit
from vectapilot_api.db import Database
from vectapilot_api.embeddings.fake import fake_vector
from vectapilot_api.kb.service import KbService
from vectapilot_api.kb.types import ReindexResult
from vectapilot_api.retrieval.types import SearchHit, VectorQuery

PRINTED: Final = "(business_id = NULLIF(current_setting('app.business_id'::text, true), ''::text))"
"""How Postgres prints ADR 0009's policy back (`pg_get_expr`): parenthesised, NULLIF upper-case."""

SAFE_ROLE: Final = RoleFacts(False, False, False, False, 0)
A: Final = Planted("canary-a-0001", "canary-aaaaaaaaaaaaaaaa")
B: Final = Planted("canary-b-0002", "canary-bbbbbbbbbbbbbbbb")


# ── Relations: RLS enabled and forced ───────────────────────────────────────────────────────


def test_a_forced_table_passes() -> None:
    assert relation_problems([Relation("chunks", "r", True, True)]) == []


def test_dropping_force_is_a_problem() -> None:
    assert relation_problems([Relation("chunks", "r", True, False)]) == [
        "chunks: row-level security is not forced (its owner skips it)"
    ]


def test_rls_disabled_is_a_problem() -> None:
    assert relation_problems([Relation("chunks", "p", False, True)]) == [
        "chunks: row-level security is not enabled"
    ]


def test_finding_no_table_is_a_problem() -> None:
    assert relation_problems([]) != []


@pytest.mark.parametrize(
    ("options", "safe"),
    [
        ((), False),
        (("security_invoker=false",), False),
        (("security_invoker=true",), True),
        (("check_option=local", "security_invoker=on"), True),
    ],
)
def test_a_view_must_run_as_the_caller(options: tuple[str, ...], safe: bool) -> None:
    problems = relation_problems([Relation("chunk_view", "v", False, False, options)])
    assert (problems == []) is safe


@pytest.mark.parametrize("kind", ["m", "f", "x"])
def test_kinds_rls_cannot_cover_are_problems(kind: str) -> None:
    assert relation_problems([Relation("other", kind, True, True)]) != []


# ── Policies: exactly ADR 0009's ─────────────────────────────────────────────────────────────


def test_the_business_policy_passes_as_postgres_prints_it() -> None:
    policy = Policy("chunks", "business_isolation", True, PRINTED, PRINTED)
    assert policy_problems(["chunks"], [policy]) == []


def test_without_with_check_the_using_rule_is_enough() -> None:
    policy = Policy("chunks", "read", True, PRINTED, None)
    assert policy_problems(["chunks"], [policy]) == []


@pytest.mark.parametrize(
    ("using", "check"),
    [
        (f"({PRINTED} OR true)", PRINTED),  # contains the rule, but OR'ed open
        (PRINTED, "true"),  # writes anywhere
        ("(business_id = current_setting('app.tenant'::text, true))", PRINTED),
        ("(true)", None),
        (None, None),
    ],
)
def test_a_weaker_permissive_policy_is_a_problem(using: str | None, check: str | None) -> None:
    policies = [
        Policy("chunks", "business_isolation", True, PRINTED, PRINTED),
        Policy("chunks", "extra", True, using, check),
    ]
    problems = policy_problems(["chunks"], policies)
    assert len(problems) == 1 and "extra" in problems[0]


def test_a_restrictive_policy_only_narrows() -> None:
    policies = [
        Policy("chunks", "business_isolation", True, PRINTED, PRINTED),
        Policy("chunks", "approved_only", False, "(status = 'approved'::text)", None),
    ]
    assert policy_problems(["chunks"], policies) == []


def test_a_table_without_a_permissive_policy_is_a_problem() -> None:
    other_table = Policy("documents", "business_isolation", True, PRINTED, PRINTED)
    assert policy_problems(["chunks"], [other_table]) != []


# ── Unique and exclusion keys start with business_id ────────────────────────────────────────


def test_a_key_starting_with_business_id_passes() -> None:
    key = KeyIndex("chunks", "chunks_key", False, ("business_id", "document_id", "chunk_index"))
    assert key_problems([key]) == []


def test_a_key_without_business_id_first_is_a_problem() -> None:
    # PR 1's first CI run: UNIQUE (document_id, chunk_index) on chunks.
    key = KeyIndex(
        "chunks",
        "chunks_document_id_chunk_index_key",
        False,
        ("document_id", "chunk_index", "business_id"),
    )
    problems = key_problems([key])
    assert len(problems) == 1 and "chunks_document_id_chunk_index_key" in problems[0]


def test_an_expression_key_is_a_problem() -> None:
    assert key_problems([KeyIndex("documents", "lower_title", False, (None,))]) != []


def test_a_random_uuid_primary_key_is_the_one_exception() -> None:
    key = KeyIndex("chunks", "chunks_pkey", True, ("id",), "uuid", "gen_random_uuid()")
    assert key_problems([key]) == []


@pytest.mark.parametrize(
    "key",
    [
        KeyIndex("t", "not_primary", False, ("id",), "uuid", "gen_random_uuid()"),
        KeyIndex("t", "chosen_by_caller", True, ("id",), "uuid", None),
        KeyIndex("t", "natural", True, ("source_id",), "text", None),
        KeyIndex("t", "two_columns", True, ("id", "x"), "uuid", "gen_random_uuid()"),
    ],
    ids=lambda key: key.name,
)
def test_other_keys_without_business_id_are_problems(key: KeyIndex) -> None:
    assert key_problems([key]) != []


APPROVED_ONLY: Final = "(status = 'approved'::text)"
"""How Postgres prints migration 0004's WHERE back (`pg_get_expr(indpred, indrelid)`)."""


def test_a_partial_key_starting_with_business_id_passes() -> None:
    # Migration 0004: one approved version per source (ADR 0010).
    key = KeyIndex(
        "documents",
        "documents_one_approved_version",
        False,
        ("business_id", "source_id"),
        "text",
        None,
        APPROVED_ONLY,
    )
    assert key_problems([key]) == []


def test_a_partial_key_without_business_id_first_is_a_problem_and_shows_its_predicate() -> None:
    # The predicate narrows which rows take part; those rows are still compared across businesses.
    key = KeyIndex(
        "documents",
        "approved_by_source",
        False,
        ("source_id", "business_id"),
        "text",
        None,
        APPROVED_ONLY,
    )
    problems = key_problems([key])
    assert len(problems) == 1
    assert "approved_by_source" in problems[0] and f"WHERE {APPROVED_ONLY}" in problems[0]


def test_a_predicate_never_makes_the_random_uuid_exception_apply() -> None:
    key = KeyIndex("t", "partial_uuid", True, ("id",), "uuid", "gen_random_uuid()", "(x > 0)")
    assert key_problems([key]) != []


# ── Foreign keys into business tables pair business_id ──────────────────────────────────────


def test_a_composite_foreign_key_passes() -> None:
    key = ForeignKey(
        "chunks", "fk", ("business_id", "document_id"), "documents", ("business_id", "id")
    )
    assert foreign_key_problems([key], ["chunks", "documents"]) == []


def test_a_foreign_key_on_the_id_alone_is_a_problem() -> None:
    key = ForeignKey("chunks", "fk", ("document_id",), "documents", ("id",))
    assert foreign_key_problems([key], ["chunks", "documents"]) != []


def test_a_foreign_key_into_a_shared_table_is_not_checked() -> None:
    key = ForeignKey("documents", "fk", ("language",), "languages", ("code",))
    assert foreign_key_problems([key], ["documents"]) == []


# ── The app role ─────────────────────────────────────────────────────────────────────────────


def test_a_safe_app_role_passes() -> None:
    assert app_role_problems(SAFE_ROLE) == []


@pytest.mark.parametrize(
    ("facts", "problem"),
    [
        (RoleFacts(True, False, False, False, 0), "is a superuser"),
        (RoleFacts(False, True, False, False, 0), "has BYPASSRLS"),
        (RoleFacts(False, False, True, False, 0), "has CREATEROLE"),
        (RoleFacts(False, False, False, True, 0), "is a member of a superuser or BYPASSRLS role"),
        (
            RoleFacts(False, False, False, False, 3),
            "owns 3 object(s), or is a member of their owner",
        ),
    ],
)
def test_each_unsafe_attribute_is_a_problem(facts: RoleFacts, problem: str) -> None:
    assert app_role_problems(facts) == [problem]


def test_a_missing_app_role_is_a_problem() -> None:
    assert app_role_problems(None) == ["the app role does not exist"]


# ── Canary leaks ─────────────────────────────────────────────────────────────────────────────


def test_own_rows_only_pass() -> None:
    assert canary_leaks({"sql:chunks": f'{{"business_id": "{A.business_id}"}}'}, A, B) == []


@pytest.mark.parametrize("marker", [B.business_id, B.canary])
def test_the_other_businesss_id_or_canary_is_a_leak(marker: str) -> None:
    outputs = {"sql:chunks": f"{A.business_id} {marker}"}
    assert canary_leaks(outputs, A, B) == [f"sql:chunks: shows {B.business_id}'s data ({marker})"]


@pytest.mark.parametrize("outputs", [{}, {"sql:chunks": ""}], ids=["no path", "empty"])
def test_seeing_nothing_at_all_is_not_a_pass(outputs: dict[str, str]) -> None:
    assert canary_leaks(outputs, A, B) != []


# ── Read paths: a [HAND-WRITE] stub is a strict xfail until written ──────────────────────────


def test_every_stub_dependency_names_a_read_path_and_every_pending_stub_is_used() -> None:
    assert READ_PATHS.keys() >= NEEDS_HAND_WRITE.keys()
    # A pending stub is run by a read path, or named as needed elsewhere (H2: PR 8's harness).
    assert set(NEEDS_HAND_WRITE.values()) | NEEDED_OUTSIDE_READ_PATHS.keys() >= PENDING_HAND_WRITE
    assert not NEEDED_OUTSIDE_READ_PATHS.keys() & set(NEEDS_HAND_WRITE.values())
    # PR 6's routes run H1: they wait for the same switch as H1's own read path.
    assert {name for name, stub in NEEDS_HAND_WRITE.items() if stub == "vector_search"} == {
        "vector_search",
        "GET /v1/kb/search",
        "POST /v1/answer",
    }


def test_only_read_paths_on_a_pending_stub_are_marked_and_each_as_a_strict_xfail() -> None:
    marked: dict[str, list[dict[str, object]]] = {}
    for param in read_path_params():
        values, marks = param.values, param.marks  # type: ignore[attr-defined]
        marked[values[0]] = [dict(mark.kwargs) for mark in marks if mark.name == "xfail"]

    assert sorted(marked) == sorted(READ_PATHS)
    for name, xfails in marked.items():
        if NEEDS_HAND_WRITE.get(name) in PENDING_HAND_WRITE:
            assert len(xfails) == 1, name
            assert (xfails[0]["strict"], xfails[0]["raises"]) == (True, NotImplementedError)
        else:
            assert xfails == [], name


ApiReadPath = Callable[[AsyncEngine, Planted], Awaitable[Mapping[str, str]]]
API_READ_PATHS: Final[list[ApiReadPath]] = [search_through_the_api, answer_through_the_api]


@pytest.fixture
def app_without_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR 6's API read paths run the real app; here everything that would reach Postgres is
    replaced (the registry, the reindex, the transaction, the stored models), and H1's
    `vector_search` stays whatever the test sets. The same paths run against Postgres in CI."""

    async def business_exists(self: Database, business_id: str) -> bool:
        return True

    async def reindex(self: KbService, business_id: str) -> ReindexResult:
        return ReindexResult(
            embedding_model="fake-embedding-1024", dimension=1024, chunks_reindexed=1
        )

    async def one_model(connection: AsyncConnection, business_id: str) -> frozenset[str]:
        return frozenset({"fake-embedding-1024"})

    monkeypatch.setattr("tests.canary.read_paths.Settings", make_settings)  # hermetic: no .env
    monkeypatch.setattr(Database, "business_exists", business_exists)
    monkeypatch.setattr(Database, "business_transaction", FakeDatabase().business_transaction)
    monkeypatch.setattr(KbService, "reindex", reindex)
    monkeypatch.setattr("vectapilot_api.retrieval.search.stored_models_in", one_model)


def search_returning(
    hits: Mapping[str, list[SearchHit]], asked: list[VectorQuery] | None = None
) -> Any:
    async def search(connection: AsyncConnection, query: VectorQuery) -> list[SearchHit]:
        if asked is not None:
            asked.append(query)
        return hits.get(query.business_id, [])

    return search


def bait(owner: Planted, score: float = 1.0) -> SearchHit:
    """`owner`'s bait chunk as a search would return it from test_leakage.plant's rows: the shared
    `BAIT` text, titled with `owner`'s canary. Asking `BAIT`, CI scores every bait 1.0."""
    return hit(BAIT, score, source_id="bait", title=owner.canary)


@pytest.mark.anyio
@pytest.mark.parametrize("path", API_READ_PATHS)
@pytest.mark.usefixtures("app_without_postgres")
async def test_an_api_read_path_lets_h1s_stub_through_so_its_xfail_holds(path: ApiReadPath) -> None:
    with pytest.raises(NotImplementedError):
        await path(cast(AsyncEngine, None), A)


@pytest.mark.anyio
@pytest.mark.parametrize("path", API_READ_PATHS)
@pytest.mark.usefixtures("app_without_postgres")
async def test_an_api_read_path_over_a_correct_search_passes_the_leak_check(
    path: ApiReadPath, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = {A.business_id: [bait(A)], B.business_id: [bait(B)]}
    asked: list[VectorQuery] = []
    monkeypatch.setattr(
        "vectapilot_api.retrieval.search.vector_search", search_returning(hits, asked)
    )

    outputs = await path(cast(AsyncEngine, None), A)

    assert canary_leaks(outputs, A, B) == []
    # The path asks the shared bait, so in CI the other business's bait is a same-model candidate
    # that scores 1.0: there is something to leak (zara's Z1).
    assert [(q.business_id, q.embedding) for q in asked] == [
        (A.business_id, fake_vector(BAIT, 1024))
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("path", API_READ_PATHS)
@pytest.mark.usefixtures("app_without_postgres")
async def test_red_proof_an_api_read_path_over_a_leaking_search_is_caught(
    path: ApiReadPath, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What CI's bait gives a route that leaks: both baits at 1.0, the other one titled B's canary.
    leaking = {A.business_id: [bait(A), bait(B)]}
    monkeypatch.setattr("vectapilot_api.retrieval.search.vector_search", search_returning(leaking))

    outputs = await path(cast(AsyncEngine, None), A)

    assert any(B.canary in problem for problem in canary_leaks(outputs, A, B))


def test_a_written_stub_has_no_marks_and_a_pending_one_exactly_one() -> None:
    assert pending_hand_write("a-stub-that-was-written") == []
    for stub in PENDING_HAND_WRITE:
        [mark] = pending_hand_write(stub)
        assert (mark.name, mark.kwargs["strict"], mark.kwargs["raises"]) == (
            "xfail",
            True,
            NotImplementedError,
        )


def test_several_stubs_give_one_mark_while_any_is_pending_and_none_once_all_are_written() -> None:
    assert pending_hand_writes("a-stub-that-was-written", "another-written-one") == []
    for stub in PENDING_HAND_WRITE:
        [mark] = pending_hand_writes("a-stub-that-was-written", stub)
        assert (mark.name, mark.kwargs["strict"], mark.kwargs["raises"]) == (
            "xfail",
            True,
            NotImplementedError,
        )
        assert stub in mark.kwargs["reason"]
    assert len(pending_hand_writes(*PENDING_HAND_WRITE)) == 1


# ── The CI gate (scripts/canary_gate.py) ─────────────────────────────────────────────────────


def junit(*cases: str) -> str:
    return f'<?xml version="1.0"?><testsuites><testsuite>{"".join(cases)}</testsuite></testsuites>'


def case(module: str, name: str, child: str = "") -> str:
    return f'<testcase classname="{module}" name="{name}">{child}</testcase>'


PASSED: Final = case("tests.canary.test_leakage", "test_ok")
XFAIL: Final = case("tests.canary.test_leakage", "test_gap", '<skipped type="pytest.xfail"/>')
SKIP: Final = case("tests.canary.test_leakage", "test_skip", '<skipped type="pytest.skip"/>')
UNIT: Final = case("tests.test_canary_checks", "test_unit")


def test_the_gate_passes_when_a_canary_test_passed() -> None:
    assert gate_problems(suite_counts(junit(PASSED, XFAIL, UNIT))) == []


@pytest.mark.parametrize(
    "report",
    [
        junit(),
        junit(UNIT),
        junit(XFAIL),
        junit(PASSED, SKIP),
        junit(case("tests.canary.test_leakage", "test_bad", "<failure/>")),
    ],
    ids=["empty", "only unit tests", "only xfail", "a skip", "only a failure"],
)
def test_the_gate_fails_closed(report: str) -> None:
    assert gate_problems(suite_counts(report)) != []


def test_skips_are_named() -> None:
    assert suite_counts(junit(SKIP)) == SuiteCounts(0, ["tests.canary.test_leakage::test_skip"])


def test_the_gate_script_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good, bad = tmp_path / "good.xml", tmp_path / "bad.xml"
    good.write_text(junit(PASSED), encoding="utf-8")
    bad.write_text("not xml", encoding="utf-8")

    assert main(["gate", str(good)]) == 0
    assert main(["gate", str(bad)]) == 1
    assert main(["gate", str(tmp_path / "missing.xml")]) == 1
    assert main(["gate"]) == 1
    assert "1 canary tests passed" in capsys.readouterr().out

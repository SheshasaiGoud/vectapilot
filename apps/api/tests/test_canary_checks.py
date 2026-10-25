"""The canary suite's checks and its CI gate, without a database: each one can turn red.

tests/canary/checks.py decides on catalog facts; these tests hand it broken facts (FORCE off, a
key without business_id, an owner role, a leaked canary) and expect a problem. The same checks run
on the real catalog, and break real tables on purpose, in tests/canary/test_leakage.py (CI).
Kept outside tests/canary/ on purpose: scripts/canary_gate.py must not count these as the suite.
"""

import ast
import html
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from scripts.canary_gate import (
    SuiteCounts,
    gate_problems,
    main,
    pending_reason,
    pending_stubs,
    suite_counts,
    summary,
)
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
    BAIT_WORD,
    NEEDED_OUTSIDE_READ_PATHS,
    NEEDS_HAND_WRITE,
    OWNER_CONNECTION_PATHS,
    PENDING_HAND_WRITE,
    READ_PATHS,
    answer_through_the_api,
    answer_through_the_api_hybrid,
    hybrid_search_as_staff,
    pending_hand_write,
    pending_hand_writes,
    read_path_params,
    search_through_the_api,
    search_through_the_api_hybrid,
)
from tests.conftest import make_settings
from tests.retrieval.standins import FakeDatabase, StandInFuse, hit
from vectapilot_api.db import Database
from vectapilot_api.embeddings.fake import fake_vector
from vectapilot_api.kb.service import KbService
from vectapilot_api.kb.types import ReindexResult
from vectapilot_api.retrieval.types import SearchHit, TextQuery, VectorQuery

PRINTED: Final = "(business_id = NULLIF(current_setting('app.business_id'::text, true), ''::text))"
"""How Postgres prints ADR 0009's policy back (`pg_get_expr`): parenthesised, NULLIF upper-case."""

SAFE_ROLE: Final = RoleFacts(False, False, False, False, 0)
TESTS: Final = Path(__file__).parent
REPO_ROOT: Final = Path(__file__).parents[3]
CORPORA: Final = (REPO_ROOT / "data" / "sample", REPO_ROOT / "evals" / "datasets")
"""What gets ingested or asked: the sample corpus and the eval datasets."""
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


API_ROUTE: Final = re.compile(r"(GET|POST|PUT|PATCH|DELETE) /")
"""How a read path that goes through the real app is named (`"GET /v1/kb/search"`)."""


def runs(stub: str) -> set[str]:
    """The read paths that run `stub`."""
    return {name for name, stubs in NEEDS_HAND_WRITE.items() if stub in stubs}


def test_every_stub_dependency_names_a_read_path_and_every_pending_stub_is_used() -> None:
    assert READ_PATHS.keys() >= NEEDS_HAND_WRITE.keys()
    assert all(NEEDS_HAND_WRITE.values()), "a read path in NEEDS_HAND_WRITE names a stub"
    run_by_read_paths = frozenset[str]().union(*NEEDS_HAND_WRITE.values())
    # A pending stub is run by a read path, or named as needed elsewhere (H2: its own tests).
    assert run_by_read_paths | NEEDED_OUTSIDE_READ_PATHS.keys() >= PENDING_HAND_WRITE
    assert not NEEDED_OUTSIDE_READ_PATHS.keys() & run_by_read_paths
    # PR 6's routes run M2 H1: they wait for the same switch as its own read path. M3 PR 4's
    # hybrid paths run both legs and H2, so each waits for every one of the three switches, and
    # rrf_fuse is no longer needed outside the read paths.
    hybrid = {"hybrid_search", "GET /v1/kb/search (hybrid)", "POST /v1/answer (hybrid)"}
    assert runs("vector_search") == {
        "vector_search",
        "GET /v1/kb/search",
        "POST /v1/answer",
        *hybrid,
    }
    assert runs("text_search") == {"text_search", *hybrid}
    assert runs("rrf_fuse") == hybrid
    assert NEEDED_OUTSIDE_READ_PATHS == {}


def test_every_search_leg_also_runs_on_the_owners_connection() -> None:
    # Every read path that is neither the whole-table SQL nor an API route runs a statement
    # with its own business clause, so it also runs where RLS cannot help (M3 PR 4 added
    # hybrid_search: both legs' clauses, through the fusion).
    legs = {name for name in READ_PATHS if name != "sql" and not API_ROUTE.match(name)}
    assert (
        legs == OWNER_CONNECTION_PATHS.keys() == {"vector_search", "text_search", "hybrid_search"}
    )
    # The very read path the app role runs, not a copy that could drift from it.
    assert all(OWNER_CONNECTION_PATHS[name] is READ_PATHS[name] for name in legs)


def marking_problems(
    params: Sequence[object], needs: Mapping[str, frozenset[str]], pending: frozenset[str]
) -> list[str]:
    """A read path whose stubs include a pending one carries exactly one mark, the strict xfail on
    NotImplementedError with the gate's reason for those stubs; any other path carries none."""
    problems: list[str] = []
    for param in params:
        values, marks = cast(Any, param).values, cast(Any, param).marks
        name = values[0]
        xfails = [(mark.args, dict(mark.kwargs)) for mark in marks if mark.name == "xfail"]
        waiting = sorted(needs.get(name, frozenset()) & pending)
        if not waiting:
            if xfails:
                problems.append(f"{name}: marked xfail, but runs no pending stub")
            continue
        expected = {
            "strict": True,
            "raises": NotImplementedError,
            "reason": pending_reason(waiting),
        }
        if xfails != [((), expected)]:
            problems.append(f"{name}: runs pending {', '.join(waiting)} without its strict xfail")
    return problems


@pytest.mark.parametrize("names", [READ_PATHS, OWNER_CONNECTION_PATHS], ids=["all", "owner"])
def test_only_read_paths_on_a_pending_stub_are_marked_and_each_as_a_strict_xfail(
    names: Mapping[str, object],
) -> None:
    params = read_path_params(names)

    assert [cast(Any, param).values[0] for param in params] == sorted(names)
    assert marking_problems(params, NEEDS_HAND_WRITE, PENDING_HAND_WRITE) == []


STRICT_PENDING: Final = pytest.mark.xfail(
    strict=True, raises=NotImplementedError, reason=pending_reason(["text_search"])
)
"""Built here by hand (outside tests/canary), so the red proofs below stay the same once H1 is
written and `pending_hand_writes("text_search")` gives no mark any more."""


@pytest.mark.parametrize(
    ("param", "problem"),
    [
        (
            pytest.param("text_search"),
            "text_search: runs pending text_search without its strict xfail",
        ),
        (
            pytest.param(
                "text_search", marks=pytest.mark.xfail(reason=pending_reason(["text_search"]))
            ),
            "text_search: runs pending text_search without its strict xfail",
        ),
        (
            pytest.param("text_search", marks=[STRICT_PENDING, STRICT_PENDING]),
            "text_search: runs pending text_search without its strict xfail",
        ),
        (pytest.param("sql", marks=STRICT_PENDING), "sql: marked xfail, but runs no pending stub"),
    ],
    ids=["no mark", "not strict, any exception", "two marks", "a path on no pending stub"],
)
def test_red_proof_a_path_on_a_pending_stub_without_its_strict_xfail_fails(
    param: object, problem: str
) -> None:
    needs = {"text_search": frozenset({"text_search"}), "sql": frozenset[str]()}
    pending = frozenset({"text_search"})

    assert (
        marking_problems([pytest.param("text_search", marks=STRICT_PENDING)], needs, pending) == []
    )
    assert marking_problems([param], needs, pending) == [problem]


# ── No xfail in tests/canary/ but the pending helper's ─────────────────────────────────────
# pytest's JUnit report keeps an xfail's reason only, so the gate cannot tell the helper's mark
# from a hand-made one with the same reason but no `raises` (it would turn a leak's AssertionError
# into an "expected failure"), or from `pytest.xfail(<that reason>)` called inside a test. So no
# `xfail` may be written in tests/canary/, or in the conftest that applies to it, except inside
# `pending_hand_writes`. Not covered: an xfail built by obfuscation (`getattr` on a computed
# name), or one added by a plugin outside these files; review catches those.

HELPER: Final = ("read_paths.py", "pending_hand_writes")
"""The one place allowed to make an xfail: (file name, function name)."""


def xfail_sites(source: str, path: str) -> list[str]:
    """Each place in `source` that names `xfail` (an attribute, a name, an import, a string),
    outside `pending_hand_writes` in read_paths.py, as `path:line`."""
    tree = ast.parse(source, path)
    allowed: set[int] = set()
    if Path(path).name == HELPER[0]:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == HELPER[1]:
                allowed |= {id(inner) for inner in ast.walk(node)}
    sites: list[str] = []
    for node in ast.walk(tree):
        names_xfail = (
            (isinstance(node, ast.Attribute) and node.attr == "xfail")
            or (isinstance(node, ast.Name) and node.id == "xfail")
            or (isinstance(node, ast.alias) and "xfail" in (node.name, node.asname))
            or (isinstance(node, ast.Constant) and node.value == "xfail")
        )
        if names_xfail and id(node) not in allowed:
            sites.append(f"{path}:{getattr(node, 'lineno', '?')}")
    return sites


def canary_sources() -> list[Path]:
    return [*sorted((TESTS / "canary").rglob("*.py")), TESTS / "conftest.py"]


def test_no_xfail_is_written_in_the_canary_suite_but_the_pending_helpers() -> None:
    files = canary_sources()
    assert len(files) > 3, "found no canary files: the check would pass by not looking"

    found = [site for f in files for site in xfail_sites(f.read_text(encoding="utf-8"), str(f))]

    assert found == []
    # The positive control: the scan does see the helper's own xfail when it is not the helper.
    helper = (TESTS / "canary" / "read_paths.py").read_text(encoding="utf-8")
    assert len(xfail_sites(helper, "elsewhere.py")) == 1


@pytest.mark.parametrize(
    "source",
    [
        '@pytest.mark.xfail(reason="flaky in CI")\ndef test_x() -> None: ...',
        f"pytestmark = pytest.mark.xfail(reason={pending_reason(['text_search'])!r})",
        'def test_x() -> None:\n    pytest.xfail("flaky in CI")',
        "from pytest import xfail",
        "from _pytest.outcomes import xfail as expected",
        'mark = getattr(pytest.mark, "xfail")',
        'params = [pytest.param("sql", marks=pytest.mark.xfail)]',
    ],
    ids=[
        "decorator",
        "copied pending reason",
        "imperative",
        "import",
        "aliased",
        "getattr",
        "param",
    ],
)
def test_red_proof_an_xfail_outside_the_helper_is_found(source: str) -> None:
    assert len(xfail_sites(source, "tests/canary/test_leakage.py")) == 1


def test_red_proof_a_second_pending_helper_elsewhere_is_found() -> None:
    source = "def pending_hand_writes() -> object:\n    return pytest.mark.xfail(strict=True)"

    assert xfail_sites(source, "tests/canary/read_paths.py") == []
    assert len(xfail_sites(source, "tests/canary/test_leakage.py")) == 1


ApiReadPath = Callable[[AsyncEngine, Planted], Awaitable[Mapping[str, str]]]
VECTOR_API_READ_PATHS: Final[list[ApiReadPath]] = [search_through_the_api, answer_through_the_api]
HYBRID_API_READ_PATHS: Final[list[ApiReadPath]] = [
    search_through_the_api_hybrid,
    answer_through_the_api_hybrid,
]
"""M3 PR 4: the same two routes with `rag_retrieval_mode=hybrid` (both legs and the fusion)."""
API_READ_PATHS: Final[list[ApiReadPath]] = [*VECTOR_API_READ_PATHS, *HYBRID_API_READ_PATHS]


@pytest.fixture
def app_without_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR 6's API read paths run the real app; here everything that would reach Postgres is
    replaced (the registry, the reindex, the transaction, the stored models); each test replaces
    H1's `vector_search` itself, and for M3 PR 4's hybrid routes M3 H1's `text_search` and M3 H2's
    `rrf_fuse` (team-side stand-ins: both are pending [HAND-WRITE]s). The same paths run against
    Postgres in CI."""

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


def text_search_returning(
    hits: Mapping[str, list[SearchHit]], asked: list[TextQuery] | None = None
) -> Any:
    """A stand-in for M3 H1's `text_search`: prepared hits per business, no SQL."""

    async def search(connection: AsyncConnection, query: TextQuery) -> list[SearchHit]:
        if asked is not None:
            asked.append(query)
        return hits.get(query.business_id, [])

    return search


def use_legs(
    monkeypatch: pytest.MonkeyPatch,
    vector: Mapping[str, list[SearchHit]],
    text: Mapping[str, list[SearchHit]],
    *,
    asked: list[VectorQuery] | None = None,
    asked_text: list[TextQuery] | None = None,
) -> None:
    """Both legs and the fusion as stand-ins, for every app built afterwards (`SearchService`
    looks them up in `retrieval.search` when the app starts). The vector-mode routes never run
    the text leg or the fusion."""
    monkeypatch.setattr(
        "vectapilot_api.retrieval.search.vector_search", search_returning(vector, asked)
    )
    monkeypatch.setattr(
        "vectapilot_api.retrieval.search.text_search", text_search_returning(text, asked_text)
    )
    monkeypatch.setattr("vectapilot_api.retrieval.search.rrf_fuse", StandInFuse())


def bait(owner: Planted, score: float = 1.0) -> SearchHit:
    """`owner`'s bait chunk as a search would return it from test_leakage.plant's rows: the shared
    `BAIT` text, titled with `owner`'s canary. Asking `BAIT`, CI scores every bait 1.0."""
    return hit(BAIT, score, source_id="bait", title=owner.canary)


@pytest.mark.anyio
@pytest.mark.parametrize("path", API_READ_PATHS)
@pytest.mark.usefixtures("app_without_postgres")
async def test_an_api_read_path_over_a_correct_search_passes_the_leak_check(
    path: ApiReadPath, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, theirs = bait(A), bait(B)
    hits = {A.business_id: [mine], B.business_id: [theirs]}
    asked: list[VectorQuery] = []
    asked_text: list[TextQuery] = []
    use_legs(monkeypatch, hits, hits, asked=asked, asked_text=asked_text)

    outputs = await path(cast(AsyncEngine, None), A)

    assert canary_leaks(outputs, A, B) == []
    # The path asks the shared bait, so in CI the other business's bait is a same-model candidate
    # that scores 1.0: there is something to leak (zara's Z1).
    assert [(q.business_id, q.embedding) for q in asked] == [
        (A.business_id, fake_vector(BAIT, 1024))
    ]
    # A hybrid route asks the full-text leg the same question, for the same business (M3 PR 4).
    hybrid = path in HYBRID_API_READ_PATHS
    expected_text = [(A.business_id, BAIT, fake_vector(BAIT, 1024))] if hybrid else []
    assert [(q.business_id, q.question, q.embedding) for q in asked_text] == expected_text


LEAKS: Final = [
    pytest.param(search_through_the_api, "vector", id="GET /v1/kb/search, vector leg"),
    pytest.param(answer_through_the_api, "vector", id="POST /v1/answer, vector leg"),
    pytest.param(search_through_the_api_hybrid, "vector", id="GET (hybrid), vector leg"),
    pytest.param(answer_through_the_api_hybrid, "vector", id="POST (hybrid), vector leg"),
    pytest.param(search_through_the_api_hybrid, "text", id="GET (hybrid), full-text leg"),
    pytest.param(answer_through_the_api_hybrid, "text", id="POST (hybrid), full-text leg"),
]
"""Every API read path with each leg it runs leaking in turn, the other leg clean (M3 PR 4)."""


@pytest.mark.anyio
@pytest.mark.parametrize(("path", "leaking_leg"), LEAKS)
@pytest.mark.usefixtures("app_without_postgres")
async def test_red_proof_an_api_read_path_over_a_leaking_search_is_caught(
    path: ApiReadPath, leaking_leg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What CI's bait gives a route that leaks: both baits at 1.0, the other one titled B's canary.
    # The hybrid routes' other leg is clean: one leg without its business clause is enough.
    mine, theirs = bait(A), bait(B)
    clean, leaking = {A.business_id: [mine]}, {A.business_id: [mine, theirs]}
    vector, text = (leaking, clean) if leaking_leg == "vector" else (clean, leaking)
    use_legs(monkeypatch, vector, text)

    outputs = await path(cast(AsyncEngine, None), A)

    assert any(B.canary in problem for problem in canary_leaks(outputs, A, B))


class ModelsConnection:
    """The connection `hybrid_search_as_staff` gets without Postgres: its one statement of its own
    (the stored-models query) answers `models`, and every statement is recorded."""

    def __init__(self, models: Sequence[str]) -> None:
        self.models = list(models)
        self.statements: list[str] = []

    async def execute(self, statement: object, parameters: object = None) -> SimpleNamespace:
        self.statements.append(str(statement))
        return SimpleNamespace(scalars=lambda: iter(self.models))


@pytest.mark.anyio
async def test_the_hybrid_read_path_runs_both_legs_and_the_fusion_in_one_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The canary suite's hybrid_search must read as `SearchService` does (D8): one business
    # transaction opened as a snapshot (REPEATABLE READ READ ONLY), both legs on its connection.
    # A read path at READ COMMITTED would pass its leak check while checking another composition.
    connection = ModelsConnection(["fake-embedding-1024"])
    opened: list[tuple[str, bool]] = []
    legs: list[tuple[str, object]] = []

    @asynccontextmanager
    async def recording(
        engine: AsyncEngine, business_id: str, *, snapshot: bool = False
    ) -> AsyncIterator[ModelsConnection]:
        opened.append((business_id, snapshot))
        yield connection

    async def vector(conn: AsyncConnection, query: VectorQuery) -> list[SearchHit]:
        legs.append(("vector", conn))
        return [bait(A)]

    async def text(conn: AsyncConnection, query: TextQuery) -> list[SearchHit]:
        legs.append(("text", conn))
        return [bait(A)]

    monkeypatch.setattr("tests.canary.read_paths.business_transaction", recording)
    monkeypatch.setattr("tests.canary.read_paths.vector_search", vector)
    monkeypatch.setattr("tests.canary.read_paths.text_search", text)
    monkeypatch.setattr("tests.canary.read_paths.rrf_fuse", StandInFuse())

    outputs = await hybrid_search_as_staff(cast(AsyncEngine, None), A)

    assert opened == [(A.business_id, True)]
    assert legs == [("vector", connection), ("text", connection)]
    assert len(connection.statements) == 1  # the stored-models query, in the same transaction
    assert canary_leaks(outputs, A, B) == []
    assert A.canary in outputs["hybrid_search:fake-embedding-1024"]


def test_the_bait_word_is_rare_and_part_of_the_bait() -> None:
    # Letters and digits (Postgres' `numword`): one unchanged lexeme in every config (M3 PR 2).
    assert re.fullmatch(r"[a-z]+[0-9][a-z0-9]*", BAIT_WORD)
    assert re.search(rf"\b{BAIT_WORD}\b", BAIT)
    # Rare: nothing that gets ingested or asked holds it, in any case (the parser lower-cases).
    word = BAIT_WORD.encode()
    files = [p for root in CORPORA for p in sorted(root.rglob("*")) if p.is_file()]
    assert files, "found no corpus files: the check would pass by not looking"
    assert [p for p in files if word in p.read_bytes().lower()] == []


def test_every_api_read_path_has_the_leaking_search_red_proof() -> None:
    # M3 PR 4's hybrid routes are in API_READ_PATHS, so the red proof above runs on them, with
    # each leg they run leaking in turn (LEAKS).
    routes = {READ_PATHS[name] for name in READ_PATHS if API_ROUTE.match(name)}
    assert routes == set(API_READ_PATHS)
    legs = {"vector_search": "vector", "text_search": "text"}
    expected = {
        (READ_PATHS[name], legs[stub])
        for name in READ_PATHS
        if API_ROUTE.match(name)
        for stub in NEEDS_HAND_WRITE[name]
        if stub in legs
    }
    assert {(param.values[0], param.values[1]) for param in LEAKS} == expected


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
    # One mark while any stub is pending; none once the last entry is removed (H1 or H2 lands last).
    assert len(pending_hand_writes(*PENDING_HAND_WRITE)) == min(1, len(PENDING_HAND_WRITE))


# ── The CI gate (scripts/canary_gate.py) ─────────────────────────────────────────────────────


def junit(*cases: str) -> str:
    return f'<?xml version="1.0"?><testsuites><testsuite>{"".join(cases)}</testsuite></testsuites>'


def case(module: str, name: str, child: str = "") -> str:
    return f'<testcase classname="{module}" name="{name}">{child}</testcase>'


def xfail(reason: str | None, name: str = "test_gap", module: str = "tests.canary.x") -> str:
    """An xfailed test case as pytest writes it (`message` is the xfail's reason)."""
    message = "" if reason is None else f' message="{html.escape(reason, quote=True)}"'
    return case(module, name, f'<skipped type="pytest.xfail"{message}/>')


READ_PATH_TEST: Final = "test_no_read_path_shows_another_businesss_canary[as a-sql]"
PASSED: Final = case("tests.canary.test_leakage", "test_ok")
PENDING: Final = xfail(pending_reason(["text_search"]))
FLAKY: Final = xfail("flaky in CI", READ_PATH_TEST, "tests.canary.test_leakage")
SKIP: Final = case("tests.canary.test_leakage", "test_skip", '<skipped type="pytest.skip"/>')
UNIT: Final = case("tests.test_canary_checks", "test_unit")


def test_the_gate_passes_when_a_canary_test_passed() -> None:
    assert gate_problems(suite_counts(junit(PASSED, PENDING, UNIT))) == []


@pytest.mark.parametrize(
    "report",
    [
        junit(),
        junit(UNIT),
        junit(PENDING),
        junit(PASSED, SKIP),
        junit(case("tests.canary.test_leakage", "test_bad", "<failure/>")),
    ],
    ids=["empty", "only unit tests", "only a pending xfail", "a skip", "only a failure"],
)
def test_the_gate_fails_closed(report: str) -> None:
    assert gate_problems(suite_counts(report)) != []


def test_skips_are_named() -> None:
    assert suite_counts(junit(SKIP)) == SuiteCounts(0, ["tests.canary.test_leakage::test_skip"])


def test_red_proof_a_flaky_xfail_on_the_read_path_test_fails_the_gate() -> None:
    # `@pytest.mark.xfail(reason="flaky in CI")` on the read-path test: a leak in the sql path
    # then reports as xfailed, and the test step stays green.
    problems = gate_problems(suite_counts(junit(PASSED, PENDING, FLAKY)))

    assert problems == [
        "xfail that is not a pending [HAND-WRITE] mark: tests.canary.test_leakage::"
        f"{READ_PATH_TEST} (reason: 'flaky in CI')"
    ]


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "",
        "flaky in CI",
        "[HAND-WRITE] text_search not written yet (M3 H1, tests/canary/read_paths.py)",
        pending_reason(["text_search"]) + " (and flaky)",
        " " + pending_reason(["text_search"]),
        pending_reason(["text_search"]).replace("text_search", ""),
        pending_reason(["text_search"]).replace("text_search", "Text_Search"),
        pending_reason(["text_search"]).replace("text_search", "text_search,rrf_fuse"),
    ],
    ids=[
        "no reason",
        "empty",
        "flaky",
        "the scaffold's own mark (a near miss)",
        "extra text",
        "leading space",
        "no stub",
        "not a function name",
        "no space after the comma",
    ],
)
def test_the_gate_refuses_every_xfail_reason_but_the_pending_helpers(reason: str | None) -> None:
    counts = suite_counts(junit(PASSED, xfail(reason)))

    assert counts.xfailed == 0
    assert len(gate_problems(counts)) == 1


def test_an_xfail_outside_the_suite_is_not_the_gates_business() -> None:
    report = junit(PASSED, xfail("flaky in CI", module="tests.test_canary_checks"))

    assert gate_problems(suite_counts(report)) == []


def test_the_helpers_reasons_are_the_ones_the_gate_accepts() -> None:
    for stub in PENDING_HAND_WRITE:
        [mark] = pending_hand_write(stub)
        assert pending_stubs(mark.kwargs["reason"]) == (stub,)
    if PENDING_HAND_WRITE:
        [mark] = pending_hand_writes(*sorted(PENDING_HAND_WRITE))
        assert pending_stubs(mark.kwargs["reason"]) == tuple(sorted(PENDING_HAND_WRITE))
    assert pending_stubs(pending_reason(["text_search", "rrf_fuse"])) == ("text_search", "rrf_fuse")


@pytest.mark.parametrize("stubs", [[], ["text search"], ["TextSearch"], ["x, y"]])
def test_a_pending_reason_names_one_or_more_function_names(stubs: list[str]) -> None:
    with pytest.raises(ValueError, match="function names"):
        pending_reason(stubs)


def test_the_gate_says_which_stubs_are_pending() -> None:
    counts = suite_counts(
        junit(PASSED, PENDING, xfail(pending_reason(["rrf_fuse", "text_search"])))
    )

    assert summary(counts) == (
        "canary gate: 1 canary tests passed; 2 xfailed on pending [HAND-WRITE] stubs"
        " (rrf_fuse, text_search)"
    )
    assert summary(suite_counts(junit(PASSED))) == "canary gate: 1 canary tests passed"


def test_the_gate_script_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good, bad, flaky = tmp_path / "good.xml", tmp_path / "bad.xml", tmp_path / "flaky.xml"
    good.write_text(junit(PASSED), encoding="utf-8")
    bad.write_text("not xml", encoding="utf-8")
    flaky.write_text(junit(PASSED, FLAKY), encoding="utf-8")

    assert main(["gate", str(good)]) == 0
    assert "1 canary tests passed" in capsys.readouterr().out
    assert main(["gate", str(bad)]) == 1
    assert main(["gate", str(tmp_path / "missing.xml")]) == 1
    assert main(["gate"]) == 1
    capsys.readouterr()
    assert main(["gate", str(flaky)]) == 1
    assert "xfail that is not a pending [HAND-WRITE] mark" in capsys.readouterr().err

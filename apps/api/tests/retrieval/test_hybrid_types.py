"""`TextQuery` and `FusedHit` (retrieval/types.py, M3 PR 3): built and tested, no database needed.

Not part of H1 or H2: these pass today. They pin what `text_search` may rely on (a valid query, its
question checked exactly like the API's `Question`) and what `rrf_fuse` must produce (a fused hit
that keeps the cosine and the RRF score apart, ADR 0014 decision 4).
"""

import uuid
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final

import pytest
from pydantic import TypeAdapter, ValidationError

from tests.retrieval.vectors import Q, vector
from vectapilot_api.retrieval.types import (
    MAX_K,
    MAX_QUESTION_CHARS,
    VISIBLE_AUDIENCES,
    FusedHit,
    Question,
    Reader,
    SearchHit,
    TextQuery,
    VectorQuery,
)

BUSINESS: Final = "sunrise-clinic"
MODEL: Final = "fake-embedding-1024"
MARKER: Final = "kq4marker"
"""A word that appears only in test questions and texts here: if it shows in a repr or an error
message, the text leaked."""
HOSTILE: Final = f"& | ! ( ) :* <-> ' \\ {MARKER}"
"""Every tsquery operator a customer could type, around the marker."""


def vector_fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "business_id": BUSINESS,
        "embedding": Q,
        "embedding_model": MODEL,
        "k": 5,
        "reader": "customer",
    }
    return fields | overrides


def text_query(**overrides: Any) -> TextQuery:
    return TextQuery.model_validate(vector_fields(question="Opening hours?") | overrides)


def problems(error: pytest.ExceptionInfo[ValidationError]) -> set[tuple[str, str]]:
    """(first location part, error type) of every problem."""
    return {(str(e["loc"][0]), e["type"]) for e in error.value.errors(include_input=False)}


# ── TextQuery ───────────────────────────────────────────────────────────────────────────────


def test_a_valid_text_query_keeps_its_values_and_strips_the_question() -> None:
    built = text_query(question="  Opening hours?\n", k=3, reader="staff")

    assert (built.business_id, built.embedding_model, built.k, built.reader) == (
        BUSINESS,
        MODEL,
        3,
        "staff",
    )
    assert built.embedding == Q
    assert built.question == "Opening hours?"  # stripped, as `Question` does for the API


@pytest.mark.parametrize("reader", ["customer", "staff"])
def test_the_audiences_follow_the_reader(reader: Reader) -> None:
    assert text_query(reader=reader).audiences == VISIBLE_AUDIENCES[reader]


@pytest.mark.parametrize(
    "overrides",
    [
        {"k": 0},
        {"k": MAX_K + 1},
        {"k": True},
        {"k": "5"},
        {"embedding": vector(2.0)},  # not unit length
        {"embedding": vector()},  # the zero vector
        {"embedding": list(Q)},  # a list, not a tuple
        {"embedding": Q[:-1]},  # one component short
        {"business_id": "Sunrise Clinic"},  # not a slug
        {"embedding_model": "sk-ant-not-a-model"},  # secret-shaped
        {"reader": "admin"},
        {"surprise": 1},  # closed model
    ],
    ids=[
        "k-zero",
        "k-over-max",
        "k-bool",
        "k-str",
        "length-2",
        "zero-vector",
        "list",
        "short",
        "business-id",
        "model-id",
        "reader",
        "extra-field",
    ],
)
def test_the_shared_fields_are_checked_exactly_like_vector_query(
    overrides: dict[str, Any],
) -> None:
    # One base class for both legs: whatever VectorQuery refuses, TextQuery refuses the same way.
    with pytest.raises(ValidationError) as vector_error:
        VectorQuery.model_validate(vector_fields(**overrides))
    with pytest.raises(ValidationError) as text_error:
        text_query(**overrides)

    assert problems(text_error) == problems(vector_error)


QUESTION: Final = TypeAdapter(Question)


@pytest.mark.parametrize(
    "question",
    [
        "",
        "   \n\t",
        "x" * (MAX_QUESTION_CHARS + 1),
        " " + "x" * MAX_QUESTION_CHARS,  # over the limit before stripping
        "lone surrogate \ud800",
        None,
        5,
    ],
    ids=[
        "empty",
        "whitespace",
        "too-long",
        "too-long-by-whitespace",
        "surrogate",
        "none",
        "int",
    ],
)
def test_a_question_the_api_refuses_is_refused(question: object) -> None:
    with pytest.raises(ValidationError):
        QUESTION.validate_python(question)  # the premise: the API's `Question` refuses it
    with pytest.raises(ValidationError) as error:
        text_query(question=question)

    assert {loc for loc, _ in problems(error)} == {"question"}


@pytest.mark.parametrize(
    "question",
    [
        HOSTILE,
        "x" * MAX_QUESTION_CHARS,
        "http://x.example/a?b=1&c=2:*",
        '-zebrafish OR "okapi"',
        "क्लिनिक कब खुलता है?",
        "clinic kab khulta hai?",
    ],
    ids=["tsquery-syntax", "max-length", "url", "websearch-syntax", "devanagari", "hinglish"],
)
def test_any_question_the_api_accepts_is_accepted_unchanged(question: str) -> None:
    # Untrusted text is data: nothing here escapes, rewrites or refuses operators. Keeping it
    # harmless is `text_search`'s job (bound, parsed in SQL, never handed to `to_tsquery`).
    assert text_query(question=question).question == QUESTION.validate_python(question) == question


def test_the_question_stays_out_of_the_repr_and_of_validation_errors() -> None:
    built = text_query(question=HOSTILE)
    assert MARKER not in repr(built)
    assert MARKER not in str(built)

    with pytest.raises(ValidationError) as error:
        text_query(question=HOSTILE + "x" * MAX_QUESTION_CHARS, k=0)
    assert MARKER not in str(error.value)
    assert MARKER not in repr(error.value)


def test_a_text_query_is_not_a_vector_query() -> None:
    # Siblings, not parent and child: mypy refuses a TextQuery where vector_search expects a
    # VectorQuery, and the other way round.
    assert not isinstance(text_query(), VectorQuery)
    assert not issubclass(VectorQuery, TextQuery)


def test_a_text_query_is_frozen() -> None:
    built = text_query()
    with pytest.raises(AttributeError):
        built.question = "something else"  # type: ignore[misc]


# ── FusedHit ────────────────────────────────────────────────────────────────────────────────


def search_hit(**overrides: Any) -> SearchHit:
    fields: dict[str, Any] = {
        "chunk_id": uuid.UUID(int=1),
        "document_id": uuid.UUID(int=2),
        "source_id": "faq-main",
        "version": "1",
        "title": "Opening hours",
        "language": "en",
        "content_kind": "faq",
        "page": None,
        "chunk_index": 0,
        "content": f"We open at 9. {MARKER}",
        "score": 0.75,
        "embedding_model": MODEL,
    }
    return SearchHit.model_validate(fields | overrides)


HIT: Final = search_hit()


def fused(**overrides: Any) -> FusedHit:
    fields: dict[str, Any] = {"hit": HIT, "rrf_score": Fraction(1, 61), "ranks": (1, None)}
    return FusedHit.model_validate(fields | overrides)


def test_a_valid_fused_hit_wraps_the_hit_itself() -> None:
    built = fused(rrf_score=Fraction(5, 6), ranks=(1, 2))

    assert built.hit is HIT  # the same object: unchanged, not a copy
    assert built.rrf_score == Fraction(5, 6)
    assert type(built.rrf_score) is Fraction
    assert built.ranks == (1, 2)


@pytest.mark.parametrize(
    "ranks",
    [(1,), (None, 3), (2, None, 7), (MAX_K, MAX_K)],
    ids=["one-list", "second-list-only", "three-lists", "max-k-in-both"],
)
def test_ranks_have_one_entry_per_list_and_none_where_absent(
    ranks: tuple[int | None, ...],
) -> None:
    assert fused(ranks=ranks).ranks == ranks


@pytest.mark.parametrize(
    "rrf_score",
    [1 / 61, 1, "1/61", True, Decimal(1) / 61, Fraction(0), Fraction(-1, 61), None],
    ids=["float", "int", "str", "bool", "decimal", "zero", "negative", "none"],
)
def test_the_rrf_score_must_be_a_positive_fraction(rrf_score: object) -> None:
    # A float would round: two sums of the same terms in another order could differ in the last
    # bit and decide a tie that is not there (ADR 0014 decision 4). Exact or nothing.
    with pytest.raises(ValidationError) as error:
        fused(rrf_score=rrf_score)

    assert {loc for loc, _ in problems(error)} == {"rrf_score"}


@pytest.mark.parametrize(
    "ranks",
    [(), (None,), (None, None), (0, None), (-1,), (True, None), (1.0,), ("1",), [1, None]],
    ids=["empty", "absent", "absent-twice", "zero", "negative", "bool", "float", "str", "list"],
)
def test_ranks_are_1_based_ints_in_a_tuple_and_at_least_one_list_holds_the_hit(
    ranks: object,
) -> None:
    with pytest.raises(ValidationError) as error:
        fused(ranks=ranks)

    assert {loc for loc, _ in problems(error)} == {"ranks"}


def test_a_fused_hit_has_no_score_so_an_rrf_score_never_meets_the_cosine_gate() -> None:
    # ADR 0014 decision 4 and D7: the evidence gate compares `hit.score` (a cosine) with
    # `rag_min_score`. An RRF score (about 0.0125-0.033 at k = 60 with candidate_k 20, 1/80 to
    # 2/61) has another name on another object, and nothing called `score` exists to be compared
    # by mistake.
    built = fused()

    assert not hasattr(built, "score")
    assert "score" not in FusedHit.model_fields
    assert built.hit.score == 0.75  # the cosine, where the gate reads it
    assert not isinstance(built, SearchHit)
    with pytest.raises(ValidationError) as error:
        fused(score=0.9)  # cannot be given one either
    assert problems(error) == {("score", "extra_forbidden")}


def test_the_repr_shows_no_chunk_text() -> None:
    built = fused()
    assert MARKER not in repr(built)
    assert MARKER not in str(built)


def test_a_fused_hit_is_frozen() -> None:
    built = fused()
    with pytest.raises(AttributeError):
        built.rrf_score = Fraction(1, 2)  # type: ignore[misc]


def test_the_hit_inside_is_validated_like_any_search_hit() -> None:
    with pytest.raises(ValidationError) as error:
        fused(hit=HIT.model_dump() | {"score": 1.5})  # not a cosine

    assert {loc for loc, _ in problems(error)} == {"hit"}

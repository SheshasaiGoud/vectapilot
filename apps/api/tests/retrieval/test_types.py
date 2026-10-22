"""`VectorQuery` and `SearchHit` (retrieval/types.py): built and tested, no database needed.

Not part of H1: these pass today. They pin what `vector_search` may rely on (a valid query) and
what it must produce (a valid hit).
"""

import math
import uuid
from typing import Any, Final

import pytest
from pydantic import ValidationError

from tests.retrieval.vectors import DIM, Q, toward, vector
from vectapilot_api.kb.types import ContentKind
from vectapilot_api.retrieval.types import MAX_K, VISIBLE_AUDIENCES, SearchHit, VectorQuery

BUSINESS: Final = "sunrise-clinic"
MODEL: Final = "fake-embedding-1024"


def query(**overrides: Any) -> VectorQuery:
    fields: dict[str, Any] = {
        "business_id": BUSINESS,
        "embedding": Q,
        "embedding_model": MODEL,
        "k": 5,
        "reader": "customer",
    }
    return VectorQuery.model_validate(fields | overrides)


def hit(**overrides: Any) -> SearchHit:
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
        "content": "We open at 9.",
        "score": 0.5,
        "embedding_model": MODEL,
    }
    return SearchHit.model_validate(fields | overrides)


def problems(error: pytest.ExceptionInfo[ValidationError]) -> set[tuple[str, str]]:
    """(first location part, error type) of every problem."""
    return {(str(e["loc"][0]), e["type"]) for e in error.value.errors(include_input=False)}


# ── VectorQuery ─────────────────────────────────────────────────────────────────────────────


def test_a_valid_query_keeps_its_values() -> None:
    built = query(k=3, reader="staff")

    assert (built.business_id, built.embedding_model, built.k, built.reader) == (
        BUSINESS,
        MODEL,
        3,
        "staff",
    )
    assert built.embedding == Q


@pytest.mark.parametrize("k", [1, MAX_K], ids=["one", "max_k"])
def test_k_from_1_to_max_k_is_accepted(k: int) -> None:
    assert query(k=k).k == k


@pytest.mark.parametrize(
    "k",
    [0, -1, MAX_K + 1, True, 5.0, "5", None],
    ids=["zero", "negative", "over-max_k", "bool", "float", "str", "none"],
)
def test_a_k_out_of_bounds_or_not_an_int_is_refused(k: object) -> None:
    with pytest.raises(ValidationError) as error:
        query(k=k)

    assert {loc for loc, _ in problems(error)} == {"k"}


@pytest.mark.parametrize("length", [0, 1, DIM - 1, DIM + 1], ids=str)
def test_a_vector_of_another_dimension_is_refused(length: int) -> None:
    embedding = (1.0,) + (0.0,) * (length - 1) if length else ()

    with pytest.raises(ValidationError) as error:
        query(embedding=embedding)

    assert ("embedding", "too_short" if length < DIM else "too_long") in problems(error)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf], ids=["nan", "inf", "-inf"])
def test_a_non_finite_component_is_refused(bad: float) -> None:
    embedding = (*Q[:3], bad, *Q[4:])

    with pytest.raises(ValidationError) as error:
        query(embedding=embedding)

    assert ("embedding", "finite_number") in problems(error)


@pytest.mark.parametrize("bad", [True, "0.0", None], ids=["bool", "str", "none"])
def test_a_component_that_is_not_a_number_is_refused(bad: object) -> None:
    with pytest.raises(ValidationError) as error:
        query(embedding=(*Q[:-1], bad))

    assert ("embedding", "float_type") in problems(error)


def test_int_components_are_accepted_as_floats() -> None:
    built = query(embedding=(1,) + (0,) * (DIM - 1))

    assert built.embedding == Q
    assert {type(x) for x in built.embedding} == {float}


def test_a_list_is_refused_only_a_tuple_is_a_vector() -> None:
    with pytest.raises(ValidationError) as error:
        query(embedding=list(Q))

    assert problems(error) == {("embedding", "tuple_type")}


@pytest.mark.parametrize(
    "embedding",
    [
        vector(),  # the zero vector: its cosine distance is undefined
        vector(2.0),  # length 2
        vector(0.5, 0.5),  # length √0.5 ≈ 0.707
        vector(1.002),  # 0.002 over: outside UNIT_NORM_TOLERANCE (0.001)
    ],
    ids=["zero", "length-2", "length-0.707", "just-outside-tolerance"],
)
def test_a_vector_that_is_not_unit_length_is_refused(embedding: tuple[float, ...]) -> None:
    with pytest.raises(ValidationError) as error:
        query(embedding=embedding)

    assert problems(error) == {("embedding", "value_error")}


@pytest.mark.parametrize(
    "embedding",
    [vector(1.0005), vector(0.6, 0.8), toward(0.75), toward(-0.5)],
    ids=["within-tolerance", "0.6-0.8", "three-quarters", "minus-half"],
)
def test_a_unit_vector_in_any_direction_is_accepted(embedding: tuple[float, ...]) -> None:
    assert query(embedding=embedding).embedding == embedding


@pytest.mark.parametrize(
    "model",
    ["", "two words", "x" * 129, "line\nbreak", "sk-ant-not-a-model"],
    ids=["empty", "space", "too-long", "newline", "looks-like-a-key"],
)
def test_a_model_id_that_is_not_id_shaped_is_refused(model: str) -> None:
    with pytest.raises(ValidationError) as error:
        query(embedding_model=model)

    assert {loc for loc, _ in problems(error)} == {"embedding_model"}


@pytest.mark.parametrize(
    "business_id",
    ["", "ab", "Sunrise-clinic", "sunrise_clinic", "sunrise-clinic\n", "x" * 41],
    ids=["empty", "too-short", "upper-case", "underscore", "trailing-newline", "too-long"],
)
def test_a_business_id_that_is_not_a_slug_is_refused(business_id: str) -> None:
    with pytest.raises(ValidationError) as error:
        query(business_id=business_id)

    assert {loc for loc, _ in problems(error)} == {"business_id"}


@pytest.mark.parametrize("reader", ["admin", "", "Customer", None], ids=repr)
def test_an_unknown_reader_is_refused(reader: object) -> None:
    with pytest.raises(ValidationError) as error:
        query(reader=reader)

    assert {loc for loc, _ in problems(error)} == {"reader"}


def test_a_customer_sees_public_only_and_staff_see_both_audiences() -> None:
    assert query(reader="customer").audiences == ("public",)
    assert query(reader="staff").audiences == ("public", "staff_only")
    assert set(VISIBLE_AUDIENCES) == {"customer", "staff"}  # every reader has an entry


def test_an_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError) as error:
        query(top_k=5)

    assert problems(error) == {("top_k", "extra_forbidden")}


def test_neither_errors_nor_repr_show_the_values() -> None:
    with pytest.raises(ValidationError) as error:
        query(business_id="Secret-Business", embedding=vector(0.123456789), k=-7)
    shown = str(error.value)

    assert "Secret-Business" not in shown and "0.123456789" not in shown and "-7" not in shown
    shown = repr(query(embedding=toward(0.75)))
    assert "embedding=" not in shown and "0.75" not in shown and "0.25" not in shown


def test_a_query_is_frozen_and_cannot_skip_validation() -> None:
    built = query()

    with pytest.raises(AttributeError):
        built.k = 10  # type: ignore[misc]
    unchecked: dict[str, Any] = {"k": 0}
    with pytest.raises(TypeError, match="model_construct"):
        VectorQuery.model_construct(**unchecked)
    with pytest.raises(ValidationError):
        built.model_copy(update={"k": 0})


# ── SearchHit ───────────────────────────────────────────────────────────────────────────────


def test_a_valid_hit_keeps_its_values() -> None:
    built = hit(page=3, chunk_index=2, score=-0.25, language="hi")

    assert (built.page, built.chunk_index, built.score) == (3, 2, -0.25)
    assert (built.chunk_id, built.document_id) == (uuid.UUID(int=1), uuid.UUID(int=2))
    assert (built.language, built.content_kind) == ("hi", ContentKind.FAQ)


@pytest.mark.parametrize("kind", list(ContentKind), ids=str)
def test_every_content_kind_is_accepted_as_the_columns_text_or_the_member(
    kind: ContentKind,
) -> None:
    # H1 reads `documents.content_kind` as text: it may pass that straight through.
    assert hit(content_kind=kind.value).content_kind is kind
    assert hit(content_kind=kind).content_kind is kind


@pytest.mark.parametrize("score", [-1.0, 0.0, 1.0, 1], ids=["-1", "0", "1", "int-1"])
def test_a_score_from_minus_1_to_1_is_accepted(score: float) -> None:
    assert hit(score=score).score == score


@pytest.mark.parametrize(
    "score",
    [1.0000001, -1.0000001, 2.0, math.nan, math.inf, "0.5", True],
    ids=["just-over-1", "just-under-minus-1", "a-distance-of-2", "nan", "inf", "str", "bool"],
)
def test_a_score_outside_minus_1_to_1_or_not_a_number_is_refused(score: object) -> None:
    with pytest.raises(ValidationError) as error:
        hit(score=score)

    assert {loc for loc, _ in problems(error)} == {"score"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("page", 0),
        ("page", True),
        ("chunk_index", -1),
        ("chunk_index", 1.0),
        ("source_id", ""),
        ("version", ""),
        ("title", ""),
        ("language", ""),
        ("content_kind", "markdown"),  # a file format, not a content kind
        ("content_kind", "FAQ"),
        ("content", ""),
        ("embedding_model", "two words"),
        ("chunk_id", "not-a-uuid"),
    ],
    ids=lambda v: str(v) if isinstance(v, str) and v else repr(v),
)
def test_a_hit_with_a_bad_field_is_refused(field: str, value: object) -> None:
    with pytest.raises(ValidationError) as error:
        hit(**{field: value})

    assert {loc for loc, _ in problems(error)} == {field}


def test_a_hit_without_a_page_is_fine_and_its_text_stays_out_of_repr() -> None:
    built = hit(page=None, content="Vaccinations need an appointment.")

    assert built.page is None
    assert "Vaccinations" not in repr(built) and "Vaccinations" not in str(built)

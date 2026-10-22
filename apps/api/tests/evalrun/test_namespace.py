"""Eval businesses per (business, embedding model, chunk config) (`evalrun/namespace.py`)."""

import re
from typing import cast

import pytest

from vectapilot_api.db import Database, parse_business_id
from vectapilot_api.evalrun.namespace import (
    NamespaceError,
    dataset_business,
    eval_business_id,
    is_eval_business_id,
    namespaces,
)
from vectapilot_api.evalrun.store import PostgresEvalStore
from vectapilot_api.ingestion.types import ChunkingConfig

DEFAULT = ChunkingConfig()


def test_an_eval_business_id_is_the_business_plus_a_stable_digest() -> None:
    first = eval_business_id("sunrise-clinic", embedding_model="voyage-4", chunking=DEFAULT)
    again = eval_business_id("sunrise-clinic", embedding_model="voyage-4", chunking=DEFAULT)

    assert first == again
    assert re.fullmatch(r"sunrise-clinic-ev-[0-9a-f]{10}", first)
    assert parse_business_id(first) == first


@pytest.mark.parametrize(
    "change",
    [
        {"embedding_model": "fake-embedding-1024"},
        {"chunking": ChunkingConfig(max_chars=400, overlap_chars=50)},
        # M17b: the size alone (overlap kept at the default 150), so the size must count itself.
        {"chunking": ChunkingConfig(max_chars=800, overlap_chars=DEFAULT.overlap_chars)},
        {"chunking": ChunkingConfig(max_chars=1200, overlap_chars=100)},
        {"salt": "test-run"},
    ],
    ids=["model", "size-and-overlap", "size-only", "overlap", "salt"],
)
def test_any_change_of_model_chunk_config_or_salt_is_another_namespace(
    change: dict[str, object],
) -> None:
    base: dict[str, object] = {"embedding_model": "voyage-4", "chunking": DEFAULT, "salt": ""}
    original = eval_business_id("sunrise-clinic", **base)  # type: ignore[arg-type]

    assert eval_business_id("sunrise-clinic", **(base | change)) != original  # type: ignore[arg-type]


def test_two_businesses_never_share_a_namespace() -> None:
    mapping = namespaces(
        ["sunrise-clinic", "paws-and-co"], embedding_model="voyage-4", chunking=DEFAULT
    )

    assert list(mapping) == ["sunrise-clinic", "paws-and-co"]
    assert len(set(mapping.values())) == 2


def test_a_business_id_too_long_for_a_namespace_is_refused_by_name() -> None:
    long_id = "a" * 27  # 27 + "-ev-" + 10 = 41 > 40

    with pytest.raises(NamespaceError, match="too long for an eval namespace"):
        eval_business_id(long_id, embedding_model="voyage-4", chunking=DEFAULT)
    assert eval_business_id("a" * 26, embedding_model="voyage-4", chunking=DEFAULT)


def test_a_row_maps_back_to_its_dataset_business_and_anything_else_to_itself() -> None:
    mapping = {"sunrise-clinic": "sunrise-clinic-ev-0123456789"}

    assert dataset_business("sunrise-clinic-ev-0123456789", mapping) == "sunrise-clinic"
    # An id that is not this run's eval namespace can never equal a question's business.
    assert dataset_business("paws-and-co-ev-0123456789", mapping) == "paws-and-co-ev-0123456789"
    assert dataset_business("sunrise-clinic", mapping) == "sunrise-clinic"


def test_only_the_derived_form_counts_as_an_eval_business_id() -> None:
    derived = eval_business_id("sunrise-clinic", embedding_model="voyage-4", chunking=DEFAULT)

    assert is_eval_business_id(derived)
    for other in (
        "sunrise-clinic",
        "sunrise-clinic-ev-",
        "sunrise-clinic-ev-0123456789a",
        "x-ev-ABCDEF0123",
    ):
        assert not is_eval_business_id(other)


@pytest.mark.anyio
@pytest.mark.parametrize("business_id", ["sunrise-clinic", "paws-and-co-ev-short"])
async def test_the_fresh_delete_refuses_any_id_that_is_not_derived_before_any_query(
    business_id: str,
) -> None:
    store = PostgresEvalStore(cast(Database, object()))  # any query would fail on `object()`

    with pytest.raises(ValueError, match=f"refusing to delete the documents of {business_id}"):
        await store.delete_documents(business_id)

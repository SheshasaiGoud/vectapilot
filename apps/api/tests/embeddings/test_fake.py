"""The deterministic fake embedder (`embeddings/fake.py`, ADR 0008)."""

import math

import pytest

from vectapilot_api.embeddings.base import Embedder, EmbeddingSpec, InputTooLarge
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.embeddings.fake import FakeEmbedder, fake_vector
from vectapilot_api.gateway.errors import InvalidRequest

BUSINESS = "sunrise-clinic"


def fake() -> FakeEmbedder:
    return FakeEmbedder(load_embedding_catalog().resolve("fake", None, 1024))


def test_same_text_same_vector_pinned_across_processes() -> None:
    # Pinned values: a change in the hash, the byte order or the scaling (any of which would
    # change every stored fake vector) turns this red. SHAKE-256, not Python's randomised `hash`.
    assert fake_vector("hello", 1024)[:3] == (
        -0.0467339791270085,
        0.042833268765352636,
        -0.04477345607790719,
    )
    assert fake_vector("नमस्ते", 1024)[:2] == (-0.025638576856593324, -0.03599100795921011)


@pytest.mark.parametrize("text", ["hello", "नमस्ते", "x" * 5_000])
def test_a_fake_vector_has_1024_finite_floats_of_unit_length(text: str) -> None:
    vector = fake_vector(text, 1024)

    assert len(vector) == 1024
    assert all(isinstance(x, float) and math.isfinite(x) for x in vector)
    assert math.isclose(math.sqrt(math.sumprod(vector, vector)), 1.0, abs_tol=1e-9)


def test_different_texts_give_different_vectors() -> None:
    texts = ["hello", "hello ", "Hello", "hallo", "नमस्ते"]

    assert len({fake_vector(text, 1024) for text in texts}) == len(texts)


@pytest.mark.anyio
async def test_the_fake_implements_the_protocol_and_ignores_input_type() -> None:
    embedder: Embedder = fake()  # mypy checks the Protocol here
    documents = await embedder.embed(
        ["a chunk", "b chunk"], input_type="document", business_id=BUSINESS
    )
    query = await embedder.embed(["a chunk"], input_type="query", business_id=BUSINESS)

    assert (documents.model, documents.dimension, documents.requests) == (
        "fake-embedding-1024",
        1024,
        1,
    )
    assert (documents.business_id, documents.input_type, documents.total_tokens) == (
        BUSINESS,
        "document",
        0,
    )
    assert query.vectors[0] == documents.vectors[0]  # a question repeating a chunk finds it
    assert documents.vectors[0] != documents.vectors[1]
    assert "vectors" not in repr(documents)
    await embedder.aclose()


@pytest.mark.anyio
async def test_the_fake_batches_and_refuses_like_production() -> None:
    small = EmbeddingSpec(
        model="fake-small",
        dimension=1024,
        max_texts_per_request=2,
        max_tokens_per_request=100,
        context_tokens=20,
    )
    embedder = FakeEmbedder(small)

    result = await embedder.embed(["a", "b", "c"], input_type="document", business_id=BUSINESS)
    assert (result.requests, len(result.vectors)) == (2, 3)

    with pytest.raises(InputTooLarge):
        await embedder.embed(["a" * 13], input_type="document", business_id=BUSINESS)
    with pytest.raises(InvalidRequest):
        await embedder.embed(["a"], input_type="document", business_id="Not A Business")
    assert embedder.calls == [("document", 3)]  # refused calls are not recorded

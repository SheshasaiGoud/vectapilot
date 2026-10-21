"""The embedding catalog (`models.toml`) and `build_embedder` (ADR 0008)."""

import re
from pathlib import Path
from typing import Any

import httpx2
import pytest

from tests.conftest import make_settings
from vectapilot_api.embeddings.base import STORED_DIMENSION
from vectapilot_api.embeddings.catalog import (
    EmbeddingCatalog,
    EmbeddingCatalogError,
    load_embedding_catalog,
    parse_embedding_catalog,
)
from vectapilot_api.embeddings.factory import EmbedderConfigError, build_embedder
from vectapilot_api.embeddings.fake import FakeEmbedder
from vectapilot_api.embeddings.voyage import VoyageEmbedder

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions"
CANARY = "canary-key-7f3a"


def catalog_dict(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = load_embedding_catalog().model_dump(mode="json")
    return data | overrides


def test_the_shipped_catalog_matches_adr_0008() -> None:
    catalog = load_embedding_catalog()

    assert catalog.providers["voyage"].default_model == "voyage-4"
    voyage = catalog.models["voyage-4"]
    assert (voyage.max_texts_per_request, voyage.max_tokens_per_request) == (1_000, 320_000)
    assert (voyage.context_tokens, voyage.dimensions) == (32_000, (256, 512, 1024, 2048))
    assert catalog.models["voyage-4-lite"].max_tokens_per_request == 1_000_000
    assert catalog.providers["fake"].default_model == "fake-embedding-1024"


def test_the_stored_dimension_is_the_chunks_column() -> None:
    # Drift guard: the constant and the migration's column type must agree.
    sql = "".join(path.read_text() for path in sorted(MIGRATION.glob("*.py")))

    assert re.findall(r"embedding vector\((\d+)\)", sql) == [str(STORED_DIMENSION)]


@pytest.mark.parametrize("provider", ["fake", "voyage"])
def test_resolve_defaults_to_the_providers_model(provider: Any) -> None:
    catalog = load_embedding_catalog()
    spec = catalog.resolve(provider, None, 1024)

    assert spec.model == catalog.providers[provider].default_model
    assert spec.dimension == 1024


@pytest.mark.parametrize(
    ("provider", "model", "dimension", "message"),
    [
        ("voyage", "voyage-9", 1024, "embeddings_model must be a model in models.toml"),
        ("fake", "voyage-4", 1024, "embeddings_model must be a model of embeddings_provider"),
        ("voyage", "fake-embedding-1024", 1024, "must be a model of embeddings_provider"),
        ("voyage", "voyage-4", 768, "embeddings_dimension must be one the model supports"),
        ("voyage", "voyage-4", 2048, "embeddings_dimension must be 1024, the chunks.embedding"),
    ],
    ids=["unknown", "fake-labelled-voyage", "voyage-labelled-fake", "unsupported", "not-stored"],
)
def test_resolve_refuses_a_model_or_dimension_the_catalog_does_not_allow(
    provider: Any, model: str, dimension: int, message: str
) -> None:
    with pytest.raises(EmbeddingCatalogError, match=re.escape(message)):
        load_embedding_catalog().resolve(provider, model, dimension)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (catalog_dict(providers={"fake": {"default_model": "fake-embedding-1024"}}), "every"),
        (
            catalog_dict(
                providers={
                    "fake": {"default_model": "voyage-4"},
                    "voyage": {"default_model": "voyage-4"},
                }
            ),
            "providers.fake.default_model must be one of its models",
        ),
    ],
    ids=["missing-provider", "default-of-another-provider"],
)
def test_an_invalid_catalog_is_refused(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        EmbeddingCatalog.model_validate(data)


def test_a_broken_catalog_file_is_refused() -> None:
    with pytest.raises(
        EmbeddingCatalogError, match=re.escape("models.toml is not a valid catalog")
    ):
        parse_embedding_catalog("[models")


@pytest.mark.anyio
async def test_build_embedder_gives_the_fake_in_test() -> None:
    embedder = build_embedder(make_settings())

    assert isinstance(embedder, FakeEmbedder)
    assert (embedder.model, embedder.dimension) == ("fake-embedding-1024", 1024)
    await embedder.aclose()


@pytest.mark.anyio
async def test_build_embedder_gives_voyage_with_the_settings_model() -> None:
    settings = make_settings(
        environment="local",
        embeddings_provider="voyage",
        embeddings_model="voyage-4-lite",
        voyage_api_key=CANARY,
        embeddings_timeout_seconds=12.5,
    )

    embedder = build_embedder(settings)

    assert isinstance(embedder, VoyageEmbedder)
    assert (embedder.model, embedder.dimension, embedder._timeout_s) == (
        "voyage-4-lite",
        1024,
        12.5,
    )
    assert isinstance(embedder._client, httpx2.AsyncClient)
    await embedder.aclose()


def test_build_embedder_rechecks_the_key() -> None:
    # Settings refuses voyage without a key; a copy that skips validation still cannot build one.
    settings = make_settings().model_copy(
        update={"embeddings_provider": "voyage", "voyage_api_key": None}
    )

    with pytest.raises(EmbedderConfigError, match="voyage_api_key is required"):
        build_embedder(settings)

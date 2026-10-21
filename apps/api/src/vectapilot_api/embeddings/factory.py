"""Build the configured embedder (ADR 0008). PR 5 calls it at startup and closes it at shutdown."""

from vectapilot_api.config import Settings
from vectapilot_api.embeddings.base import Embedder
from vectapilot_api.embeddings.catalog import EmbeddingCatalog, load_embedding_catalog
from vectapilot_api.embeddings.fake import FakeEmbedder
from vectapilot_api.embeddings.voyage import VoyageEmbedder


class EmbedderConfigError(ValueError):
    """The settings cannot build an embedder. The message names settings, never a value."""


def build_embedder(settings: Settings, catalog: EmbeddingCatalog | None = None) -> Embedder:
    """The fake or the Voyage embedder, as Settings say. Raises `EmbeddingCatalogError` for a
    model or dimension the catalog does not allow."""
    spec = (catalog or load_embedding_catalog()).resolve(
        settings.embeddings_provider, settings.embeddings_model, settings.embeddings_dimension
    )
    if settings.embeddings_provider == "fake":
        return FakeEmbedder(spec)
    if settings.voyage_api_key is None:  # Settings already refuses this; checked again here
        raise EmbedderConfigError("voyage_api_key is required when embeddings_provider is 'voyage'")
    return VoyageEmbedder(
        api_key=settings.voyage_api_key,
        spec=spec,
        timeout_s=settings.embeddings_timeout_seconds,
    )

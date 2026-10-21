"""The embedding model catalog, from the packaged `models.toml` (ADR 0008).

`resolve` turns the three settings (provider, model, dimension) into an `EmbeddingSpec`, or raises
`EmbeddingCatalogError`. Messages name settings and rules, never a value.
"""

import tomllib
from datetime import date
from importlib.resources import files
from typing import Annotated, Final, Literal, Self, get_args

from pydantic import Field, ValidationError, model_validator

from vectapilot_api.embeddings.base import STORED_DIMENSION, EmbeddingSpec
from vectapilot_api.gateway.types import GatewayModel, OpaqueId

CATALOG_PACKAGE: Final = "vectapilot_api.embeddings"
CATALOG_FILE: Final = "models.toml"

EmbeddingsProvider = Literal["fake", "voyage"]
"""The providers Settings accepts (`VECTAPILOT_EMBEDDINGS_PROVIDER`)."""


class EmbeddingCatalogError(ValueError):
    """The catalog cannot be used, or the settings name a model it does not allow."""


class ProviderEntry(GatewayModel):
    default_model: OpaqueId


class ModelEntry(GatewayModel):
    provider: EmbeddingsProvider
    dimensions: tuple[Annotated[int, Field(gt=0, strict=True)], ...] = Field(min_length=1)
    max_texts_per_request: int = Field(gt=0, strict=True)
    max_tokens_per_request: int = Field(gt=0, strict=True)
    context_tokens: int = Field(gt=0, strict=True)
    source_url: Annotated[str, Field(pattern=r"^https://\S+$")]
    checked: date


class EmbeddingCatalog(GatewayModel):
    providers: dict[EmbeddingsProvider, ProviderEntry]
    models: dict[OpaqueId, ModelEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _every_provider_has_its_default_model(self) -> Self:
        if set(self.providers) != set(get_args(EmbeddingsProvider)):
            raise ValueError("[providers] must list every provider: fake and voyage")
        for name, entry in self.providers.items():
            model = self.models.get(entry.default_model)
            if model is None or model.provider != name:
                raise ValueError(f"providers.{name}.default_model must be one of its models")
        return self

    def resolve(
        self, provider: EmbeddingsProvider, model: str | None, dimension: int
    ) -> EmbeddingSpec:
        """The spec for these settings. `model` None means the provider's `default_model`."""
        model_id = self.providers[provider].default_model if model is None else model
        entry = self.models.get(model_id)
        if entry is None:
            raise EmbeddingCatalogError(f"embeddings_model must be a model in {CATALOG_FILE}")
        if entry.provider != provider:
            raise EmbeddingCatalogError("embeddings_model must be a model of embeddings_provider")
        if dimension not in entry.dimensions:
            raise EmbeddingCatalogError("embeddings_dimension must be one the model supports")
        if dimension != STORED_DIMENSION:
            raise EmbeddingCatalogError(
                f"embeddings_dimension must be {STORED_DIMENSION}, the chunks.embedding column"
                " (another one needs a migration and a full reindex)"
            )
        return EmbeddingSpec(
            model=model_id,
            dimension=dimension,
            max_texts_per_request=entry.max_texts_per_request,
            max_tokens_per_request=entry.max_tokens_per_request,
            context_tokens=entry.context_tokens,
        )


def parse_embedding_catalog(text: str) -> EmbeddingCatalog:
    """Parse and validate catalog TOML. Raises `EmbeddingCatalogError`."""
    try:
        return EmbeddingCatalog.model_validate(tomllib.loads(text))
    except (tomllib.TOMLDecodeError, ValidationError) as error:
        # A committed file with no caller text or secret: its errors may be shown and chained.
        raise EmbeddingCatalogError(f"{CATALOG_FILE} is not a valid catalog: {error}") from error


def load_embedding_catalog() -> EmbeddingCatalog:
    """Read and validate the catalog shipped inside this package."""
    resource = files(CATALOG_PACKAGE).joinpath(CATALOG_FILE)
    return parse_embedding_catalog(resource.read_text(encoding="utf-8"))

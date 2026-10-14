"""The model catalog: model ids, prices and aliases, from the packaged `models.toml` (ADR 0006).

The file is read with `tomllib` and `parse_float=Decimal`, then validated into typed values. TOML
has no decimal type: without `parse_float`, a price written 0.10 would arrive as the float
0.1000000000000000055…, which `types.Usd` refuses. A whole number (`3`) arrives as an int and
becomes `Decimal("3")` during validation.

`create_app` loads the catalog at startup, so an invalid file or an alias whose model has no price
stops the app before it serves anything (never a silent $0). The catalog is a committed file that
holds no caller text and no secret, so its errors may name its own keys and be chained.
"""

import tomllib
from datetime import date
from decimal import Decimal
from importlib.resources import files
from typing import Annotated, Final, Self

from pydantic import Field, ValidationError, model_validator

from vectapilot_api.gateway.types import Effort, GatewayModel, ModelPricing, OpaqueId, Slug

CATALOG_PACKAGE: Final = "vectapilot_api.gateway"
CATALOG_FILE: Final = "models.toml"


class CatalogError(ValueError):
    """The catalog cannot be used: it is not TOML, or not a valid catalog."""


class PricedModel(ModelPricing):
    """One model id: its prices (so it *is* a `ModelPricing`, ready for H1) and their source."""

    source_url: Annotated[str, Field(pattern=r"^https://\S+$")]
    checked: date
    """When the prices were last compared with `source_url`."""

    @model_validator(mode="after")
    def _every_price_is_positive(self) -> Self:
        # `Usd` allows 0 (a cost can be $0); a list price of 0 is a typo that would hide cost.
        tiers = [self.standard, *([self.long_prompt.prices] if self.long_prompt else [])]
        if any(price <= 0 for tier in tiers for price in tier.model_dump().values()):
            raise ValueError("every price must be > 0 (a $0 price would hide cost)")
        return self


class Alias(GatewayModel):
    """A name callers use (`small`, `large`): which model it calls, and how."""

    model: OpaqueId
    max_output_tokens: int = Field(gt=0, strict=True)
    """The largest `max_tokens` a call may use, and the value used when the request sets none."""
    effort: Effort


class Catalog(GatewayModel):
    """The whole catalog. Every alias points to a model with a price."""

    catalog_version: OpaqueId
    """Goes into every `llm_call` record and the cache key: change it with any edit."""
    models: dict[OpaqueId, PricedModel] = Field(min_length=1)
    aliases: dict[Slug, Alias] = Field(min_length=1)

    @model_validator(mode="after")
    def _every_alias_has_a_priced_model(self) -> Self:
        unpriced = sorted(
            name for name, alias in self.aliases.items() if alias.model not in self.models
        )
        if unpriced:
            names = ", ".join(unpriced)
            raise ValueError(f"every alias must point to a model in [models]; unpriced: {names}")
        return self


def parse_catalog(text: str) -> Catalog:
    """Parse and validate catalog TOML. Raises `CatalogError`."""
    try:
        data = tomllib.loads(text, parse_float=Decimal)
    except tomllib.TOMLDecodeError as error:
        raise CatalogError(f"{CATALOG_FILE} is not valid TOML: {error}") from error
    try:
        return Catalog.model_validate(data)
    except ValidationError as error:
        raise CatalogError(f"{CATALOG_FILE} is not a valid catalog: {error}") from error


def load_catalog() -> Catalog:
    """Read and validate the catalog shipped inside this package (the wheel includes it)."""
    return parse_catalog(files(CATALOG_PACKAGE).joinpath(CATALOG_FILE).read_text(encoding="utf-8"))

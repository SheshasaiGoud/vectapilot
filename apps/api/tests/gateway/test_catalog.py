"""The model catalog: `gateway/models.toml`, read and validated by `gateway/catalog.py`.

The prices in these tests are made up; only `test_the_packaged_catalog_is_valid` reads the real
file, and it checks structure (ADR 0006 decisions), not prices: a reviewer compares those with the
pricing page.
"""

import json
import tomllib
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Final

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from tests.conftest import UNREACHABLE_SETTINGS
from vectapilot_api import main
from vectapilot_api.gateway.catalog import Catalog, CatalogError, load_catalog, parse_catalog
from vectapilot_api.gateway.types import ModelPricing

SRC = Path(__file__).resolve().parents[2] / "src"

CATALOG_DIGESTS: Final = {
    "2026-10-14.1": "59af306e00097f1fac06fabd6a376fb9c9b9a3642bb52b567e0079a50257fc7e",
}
"""Each `catalog_version` and the SHA-256 of what it means (the parsed catalog without the version).
Keep old entries: they record which version meant which prices."""

VALID = """\
catalog_version = "test-1"

[models.model-a]
source_url = "https://example.com/pricing"
checked = 2026-10-14

[models.model-a.standard]
input = 3
output = 15
cache_read = 0.10
cache_write_5m = 3.75
cache_write_1h = 6

[aliases.small]
model = "model-a"
max_output_tokens = 100
effort = "low"
"""

MODEL_B = """
[models.model-b]
source_url = "https://example.com/pricing"
checked = 2026-10-14

[models.model-b.standard]
input = 1
output = 1
cache_read = 1
cache_write_5m = 1
cache_write_1h = 1
"""

LONG_TIER_WITH_A_ZERO = """
[models.model-a.long_prompt]
threshold_tokens = 10

[models.model-a.long_prompt.prices]
input = 6
output = 30
cache_read = 0
cache_write_5m = 7.5
cache_write_1h = 12
"""


def test_the_packaged_catalog_is_valid() -> None:
    catalog = load_catalog()

    assert set(catalog.aliases) == {"small", "large"}  # ADR 0006: no other alias in M1
    for alias in catalog.aliases.values():
        assert alias.effort == "low"  # ADR 0006: `low` on both, always sent
        assert isinstance(catalog.models[alias.model], ModelPricing)  # priced, ready for H1
    assert catalog.aliases["small"].model != catalog.aliases["large"].model


def test_any_catalog_change_needs_a_new_catalog_version() -> None:
    # `catalog_version` goes into every llm_call record and the cache key, so one version must
    # always mean one set of prices and aliases. Parsed data is hashed: a comment edit needs no
    # new version; a price, tier, alias or source change does.
    catalog = load_catalog()
    body = catalog.model_dump(mode="json", exclude={"catalog_version"})
    digest = sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    assert CATALOG_DIGESTS.get(catalog.catalog_version) == digest, (
        f"models.toml changed: give it a new catalog_version and add {digest} under it here"
    )


def test_a_whole_number_price_becomes_a_decimal() -> None:
    # TOML reads `3` as an int, not a float, so `parse_float` does not touch it: validation does.
    price = parse_catalog(VALID).models["model-a"].standard.input

    assert type(price) is Decimal
    assert price == Decimal("3")
    assert str(price) == "3"


def test_a_decimal_price_is_exact() -> None:
    price = parse_catalog(VALID).models["model-a"].standard.cache_read

    assert price == Decimal("0.10")
    assert str(price) == "0.10"  # the exact digits from the file
    assert price != Decimal(float(price))  # a float in between would give 0.1000000000000000055…


def test_a_float_price_is_refused() -> None:
    # Why `parse_float=Decimal` matters: without it, 0.10 arrives as a float and is refused.
    with pytest.raises(ValidationError, match="never a float"):
        Catalog.model_validate(tomllib.loads(VALID))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("catalog_version = ", "is not valid TOML", id="malformed"),
        pytest.param(VALID + MODEL_B + MODEL_B, "is not valid TOML", id="duplicate-model"),
        pytest.param(
            VALID + '[aliases.small]\nmodel = "model-a"\n', "is not valid TOML", id="dup-alias"
        ),
        pytest.param(
            VALID.replace("output = 15", "output = 15\noutput = 16"),
            "is not valid TOML",
            id="duplicate-price",
        ),
        pytest.param(
            VALID.replace("output = 15", "output = 15\noutptu = 15"),
            "Extra inputs are not permitted",
            id="unknown-price-key",
        ),
        pytest.param(
            VALID.replace('effort = "low"', 'effort = "low"\ntemperature = 0'),
            "Extra inputs are not permitted",
            id="unknown-alias-key",
        ),
        pytest.param(
            'version = "1"\n' + VALID, "Extra inputs are not permitted", id="unknown-top-level"
        ),
        pytest.param(
            VALID.replace("cache_write_1h = 6\n", ""), "Field required", id="missing-price-class"
        ),
        pytest.param(
            VALID.replace('model = "model-a"', 'model = "model-b"'),
            "every alias must point to a model in [models]; unpriced: small",
            id="alias-to-unpriced-model",
        ),
        pytest.param(
            VALID.replace('effort = "low"', 'effort = "none"'), "Input should be", id="bad-effort"
        ),
        pytest.param(
            VALID.replace("cache_read = 0.10", "cache_read = 0.0"),
            "every price must be > 0",
            id="zero-price",
        ),
        pytest.param(VALID + LONG_TIER_WITH_A_ZERO, "every price must be > 0", id="zero-long-tier"),
    ],
)
def test_an_invalid_catalog_is_refused(text: str, expected: str) -> None:
    with pytest.raises(CatalogError) as error:
        parse_catalog(text)

    assert expected in str(error.value)


def test_no_model_id_literal_in_python_sources() -> None:
    # ADR 0006: model ids live only in models.toml; Python code uses aliases.
    sources = sorted(SRC.rglob("*.py"))
    offenders = [p.relative_to(SRC) for p in sources if "claude-" in p.read_text("utf-8")]

    assert sources  # the search really looked somewhere
    assert offenders == []


def test_create_app_loads_the_catalog(app: FastAPI) -> None:
    assert app.state.catalog == load_catalog()


def test_create_app_stops_on_an_invalid_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "load_catalog", lambda: parse_catalog(VALID + MODEL_B + MODEL_B))

    with pytest.raises(CatalogError):
        main.create_app(UNREACHABLE_SETTINGS.model_copy())

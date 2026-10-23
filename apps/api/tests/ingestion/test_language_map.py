"""The language map (ADR 0014 decision 2): BCP 47 tag -> text-search config, without a database.

That every mapped config exists in Postgres is checked against the real catalog in
tests/kb/test_postgres_full_text.py; that migration 0005's written-out copy matches, in
tests/test_migrations.py.
"""

import re

import pytest

from vectapilot_api.ingestion.language_map import (
    FALLBACK_CONFIG,
    TEXT_SEARCH_CONFIGS,
    text_search_config,
)


def test_the_decided_map_and_fallback() -> None:
    # D2 (b), 2026-10-23: en -> english, hi -> hindi, anything else -> simple.
    assert dict(TEXT_SEARCH_CONFIGS) == {"en": "english", "hi": "hindi"}
    assert FALLBACK_CONFIG == "simple"


@pytest.mark.parametrize(
    ("language", "config"),
    [
        ("en", "english"),
        ("en-IN", "english"),  # region: stemming is by language, not country
        ("en-GB", "english"),
        ("hi", "hindi"),
        ("hi-IN", "hindi"),
        ("hi-Latn", "hindi"),  # romanized Hindi: `hindi` stems Latin words as English
        ("hi-Deva-IN", "hindi"),
        ("EN", "english"),  # the API refuses this form; a stored legacy value still maps
    ],
)
def test_the_primary_language_subtag_decides(language: str, config: str) -> None:
    assert text_search_config(language) == config


@pytest.mark.parametrize(
    "language",
    [
        "te",  # Postgres has no Telugu config
        "te-IN",
        "fr",
        "und",  # undetermined
        "eng",  # not the BCP 47 form of English (that is `en`)
        "hin",
        "",
    ],
)
def test_every_other_language_gets_the_fallback(language: str) -> None:
    assert text_search_config(language) == FALLBACK_CONFIG


def test_every_key_is_a_primary_subtag_so_the_lookup_can_reach_it() -> None:
    # The lookup uses only the primary subtag: a key such as `hi-Latn` would never match.
    assert all(re.fullmatch(r"[a-z]{2,3}", key) for key in TEXT_SEARCH_CONFIGS)

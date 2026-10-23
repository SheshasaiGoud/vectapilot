"""The language map (ADR 0014 decision 2, M3 PR 2): a document's BCP 47 tag -> its chunks'
Postgres text-search config (`chunks.ts_config`, migration 0005).

A platform file: it names no business, channel or document set (rule 9). Every business's
documents go through the same map, keyed by the language the uploader declared.

**The rule.** The tag's primary language subtag decides, compared lower-case; region, script and
variant subtags are ignored. `en`, `en-IN` and `en-GB` get `english`; `hi`, `hi-IN` and
`hi-Latn` get `hindi`; every other language (`te` included: Postgres has no Telugu config) gets
`simple`, which lower-cases words and neither stems them nor drops stop words.
- Region: Postgres' configs stem by language, never by country.
- Script, `hi-Latn` (romanized Hindi, Hinglish): `hindi` sends Latin-letter words to the English
  stemmer and Devanagari words to the Hindi one (`ts_debug('hindi', ...)`: `asciiword` ->
  `english_stem`, `word` -> `hindi_stem`; checked on the local stack, PostgreSQL 18.6,
  2026-10-23). A romanized chunk then gets exactly the lexemes `english` would give it, and a
  chunk that mixes both scripts gets both stemmers. `english` would leave Devanagari unstemmed.
- A three-letter code for a language that has a two-letter one (`eng`, `hin`) is not the BCP 47
  form, so it gets `simple`, like any language not listed.

**A change needs a migration.** A stored `tsv` does not follow a later change to this map. Any
change ships with its own backfill migration, which writes the new map out as 0005 does
(tests/test_migrations.py compares the newest copy with this file, not the rows). Such a backfill
only rewrites values that are already set, so no NOT NULL check stops it when RLS hides rows: it
must refuse to run unless the migrating role skips RLS (a `DO` block with fixed text; 0005's
docstring has one), or run once per business with the ids supplied from outside the database
(`businesses` is FORCE RLS too).

Why a Python module and not a TOML file next to the code (decided 2026-10-23, shesha; ADR 0014
decision 2, amended): two entries, typed and read by mypy, no loader or packaged-file lookup, and
nothing to parse at startup. The map cannot change without a migration anyway, so editing it
without a code change would be a hazard, not a benefit.

**A stored document's language never changes** (decided 2026-10-23, shesha): re-uploading the same
bytes and version with another tag is a `LanguageConflict` (409 `language_conflict`), compared
as stored, not through this map (`kb/service.py`).
"""

from collections.abc import Mapping
from typing import Final

TEXT_SEARCH_CONFIGS: Final[Mapping[str, str]] = {
    "en": "english",
    "hi": "hindi",
}
"""Primary language subtag -> Postgres text-search config (each must exist in `pg_catalog`).
Read-only through its type (mypy refuses a write)."""

FALLBACK_CONFIG: Final = "simple"
"""For every language not in `TEXT_SEARCH_CONFIGS`."""


def text_search_config(language: str) -> str:
    """The text-search config for chunks of a document in `language` (a BCP 47 tag)."""
    primary = language.split("-", 1)[0].lower()
    return TEXT_SEARCH_CONFIGS.get(primary, FALLBACK_CONFIG)

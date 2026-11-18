"""M7 PR 1: the guard folder's loader (G4) on synthetic fixture files.

Every refusal names the file and the location (or the business and the rule), never a term: the
mutated files below write `MARKER` where a term, a word or an unknown key goes, and no message
may contain it.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.guards.helpers import (
    BUSINESSES,
    CORPUS_DIR,
    GUARD_DIR,
    MARKER,
    copy_fixtures,
    edit_json,
    load_fixtures,
    read_json,
)
from vectapilot_api.guards.config import (
    MAX_FILE_BYTES,
    UNKNOWN_KEY,
    GuardConfig,
    GuardConfigError,
    NotCoveredCategory,
    load_guard_dir,
)
from vectapilot_api.guards.normalize import fold
from vectapilot_api.guards.types import NearTerm, PhraseTerm, TermCategory
from vectapilot_api.intents.policy import BusinessType


def refused(guard: Path, corpus: Path = CORPUS_DIR, **kwargs: Any) -> str:
    with pytest.raises(GuardConfigError) as error:
        load_guard_dir(guard, corpus, **kwargs)
    message = str(error.value)
    assert MARKER not in message.lower()
    return message


def term(**fields: Any) -> dict[str, Any]:
    """A valid English injection phrase term, changed by `fields`."""
    base: dict[str, Any] = {
        "id": "fx_test_term",
        "language": "en",
        "kind": "phrase",
        "text": f"{MARKER} words",
        "source": {"rule": "a test rule"},
    }
    return base | fields


def red_flag(**fields: Any) -> dict[str, Any]:
    return term(intent="emergency", source={"section": "4.1", "item": "a test item"}) | fields


# ── The fixture folder ───────────────────────────────────────────────────────────────────────


def test_the_fixture_folder_loads_every_business_with_its_manifest_type() -> None:
    config = load_fixtures()

    assert tuple(config.businesses) == BUSINESSES
    types = {b: g.business_type for b, g in config.businesses.items()}
    assert types == {
        "fx-clinic": BusinessType.HUMAN_HEALTHCARE,
        "fx-shop": BusinessType.ECOMMERCE,
        "fx-vet": BusinessType.VETERINARY,
    }
    assert {b: g.health for b, g in config.businesses.items()} == {
        "fx-clinic": True,
        "fx-shop": False,
        "fx-vet": True,
    }
    assert len(config.rules_version) == 64
    assert config.not_covered_categories == {
        BusinessType.HUMAN_HEALTHCARE: frozenset({NotCoveredCategory.SELF_HARM}),
        BusinessType.VETERINARY: frozenset({NotCoveredCategory.SELF_HARM}),
    }


def ids(terms: Any) -> set[str]:
    return {t.id for t in terms.terms}


def test_each_business_gets_the_platform_its_type_and_its_own_terms_only() -> None:
    config = load_fixtures()
    clinic, vet, shop = (config.for_business(b) for b in ("fx-clinic", "fx-vet", "fx-shop"))

    platform = {"fx_distress_now", "fx_distress_now_hi"}
    assert ids(shop.red_flags) == platform
    assert ids(vet.red_flags) == platform | {"fx_rat_poison", "fx_prescription_diet"}
    assert "fx_fainted_twice" in ids(clinic.red_flags)
    assert "fx_rat_poison" not in ids(clinic.red_flags)
    assert ids(clinic.output) >= {"fx_you_probably_have", "fx_booking_confirmed"}
    assert "fx_you_probably_have" not in ids(vet.output)
    assert ids(shop.injection) == {"fx_ignore_previous", "fx_ignore_previous_hi"}
    # Each list holds only its own categories.
    assert clinic.red_flags.categories() == {
        TermCategory.EMERGENCY,
        TermCategory.PROFESSIONAL_JUDGEMENT,
    }
    assert clinic.injection.categories() == {TermCategory.INJECTION}
    assert clinic.output.categories() == {
        TermCategory.SUCCESS_CLAIM,
        TermCategory.MEDICAL_ADVICE,
        TermCategory.DIAGNOSIS,
        TermCategory.ABUSE,
    }


def test_guidance_and_not_covered_come_from_the_business_file() -> None:
    config = load_fixtures()

    assert config.for_business("fx-clinic").guidance == (
        "Call the fixture clinic's emergency line, 000 (fictional).",
        "Do not wait for a reply in the chat.",
    )
    assert config.for_business("fx-shop").guidance == ()
    assert config.for_business("fx-vet").not_covered == {"te"}
    assert config.for_business("fx-clinic").not_covered == frozenset()
    assert config.scripts["te"] == ((0x0C00, 0x0C7F),)


def test_terms_are_folded_as_the_text_is() -> None:
    clinic = load_fixtures().for_business("fx-clinic")
    by_id = {t.id: t for t in clinic.red_flags.terms}

    chest = by_id["fx_chest_pain"]  # written "Chest Pain"
    assert isinstance(chest, PhraseTerm)
    assert chest.words == ("chest", "pain")
    hindi = by_id["fx_chest_pain_hi"]
    assert isinstance(hindi, PhraseTerm)
    assert hindi.text == fold("सीने में दर्द")
    near = by_id["fx_chest_near"]
    assert isinstance(near, NearTerm)
    assert near.sets == (("chest",), ("pain", "hurts", "tight"))
    assert near.window == 4


def test_a_business_without_a_file_gets_every_type_s_terms_and_no_guidance() -> None:
    config = load_fixtures()
    unresolved = config.for_business("no-such-business")

    assert unresolved is config.unresolved
    assert not unresolved.resolved
    assert unresolved.business_id is None
    assert unresolved.health  # fail towards safety: priorities as for a health business
    assert unresolved.guidance == ()
    assert ids(unresolved.red_flags) >= {"fx_chest_pain", "fx_rat_poison", "fx_distress_now"}
    assert "fx_fainted_twice" not in ids(unresolved.red_flags)  # a business's own stay its own
    assert unresolved.not_covered == {"te"}  # scripted, and no type covers it
    assert config.for_business("fx-clinic").resolved


def test_the_unresolved_set_needs_a_professional_judgement_term_per_language(
    tmp_path: Path,
) -> None:
    # Mutant g43: the unresolved set judged as a non-health business. It is treated as health, so
    # a language without a professional_judgement term is not covered there (fixture te has no
    # terms at all, so it cannot show this).
    guard, corpus = copy_fixtures(tmp_path)
    edit_json(
        guard / HUMAN,
        lambda d: d.update(red_flags=[t for t in d["red_flags"] if t["id"] != "fx_change_dose_hi"]),
    )
    edit_json(guard / CLINIC, lambda d: d.update(not_covered=["hi"]))

    assert load_guard_dir(guard, corpus).unresolved.not_covered == {"hi", "te"}


def test_a_one_code_point_script_range_is_accepted(tmp_path: Path) -> None:
    # Mutant g21 (`<` for `<=`): "first is at most last" includes first == last.
    guard, corpus = copy_fixtures(tmp_path)
    edit_json(
        guard / PLATFORM,
        lambda d: d["scripts"][1].update(ranges=[{"first": "U+0C00", "last": "U+0C00"}]),
    )

    assert load_guard_dir(guard, corpus).scripts["te"] == ((0x0C00, 0x0C00),)


def test_the_rules_version_moves_with_any_file_and_not_with_the_readme(tmp_path: Path) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    before = load_guard_dir(guard, corpus).rules_version
    (guard / "README.md").write_text("# notes\n", encoding="utf-8")
    assert load_guard_dir(guard, corpus).rules_version == before

    path = guard / "businesses" / "fx-shop.json"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert load_guard_dir(guard, corpus).rules_version != before


def test_dotfiles_are_ignored(tmp_path: Path) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    (guard / ".DS_Store").write_bytes(b"\0")
    (guard / "types" / ".hidden").write_bytes(b"\0")

    assert isinstance(load_guard_dir(guard, corpus), GuardConfig)


# ── Served businesses: the type a guarded route reads ────────────────────────────────────────


def test_a_served_health_type_makes_the_business_health_and_adds_its_terms(
    tmp_path: Path,
) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    edit_json(
        guard / "businesses" / "fx-shop.json",
        lambda d: d.update(guidance=["Call the fixture shop's line (fictional)."]),
    )

    shop = load_guard_dir(guard, corpus, served={"fx-shop": BusinessType.VETERINARY}).for_business(
        "fx-shop"
    )
    assert shop.business_type is BusinessType.ECOMMERCE  # the manifest's
    assert shop.health
    assert "fx_rat_poison" in ids(shop.red_flags)


def test_a_served_health_type_without_guidance_is_refused() -> None:
    message = refused(GUARD_DIR, served={"fx-shop": BusinessType.VETERINARY})

    assert message == "businesses/fx-shop.json: a health business has guidance lines (G3, G4)"


def test_a_served_health_business_without_a_file_is_refused() -> None:
    message = refused(GUARD_DIR, served={"sunrise-clinic": BusinessType.HUMAN_HEALTHCARE})

    assert message == (
        "sunrise-clinic: a guarded health business has no businesses/sunrise-clinic.json (its"
        " guidance, G4)"
    )


def test_a_served_other_business_without_a_file_is_unresolved_not_refused() -> None:
    config = load_guard_dir(GUARD_DIR, CORPUS_DIR, served={"rt-shop": BusinessType.ECOMMERCE})

    assert config.for_business("rt-shop") is config.unresolved


# ── Refusals: files and folders ──────────────────────────────────────────────────────────────


def test_missing_folders_are_refused(tmp_path: Path) -> None:
    assert "(VECTAPILOT_GUARD_CONFIG_DIR) is missing" in refused(tmp_path / "nowhere")
    assert "(VECTAPILOT_CORPUS_DIR) is missing" in refused(GUARD_DIR, tmp_path / "nowhere")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda g: (g / "platform.json").unlink(),
            "the guard configuration folder has no platform.json",
        ),
        (
            lambda g: (g / "notes.txt").write_text("x", encoding="utf-8"),
            "unexpected entry 'notes.txt' in the guard configuration folder",
        ),
        (
            lambda g: (g / "types" / "veterinary.json").rename(g / "types" / "veterinary.jsn"),
            "unexpected entry 'veterinary.jsn' in types",
        ),
        (
            lambda g: (g / "types" / "veterinary.json").rename(g / "types" / "pharmacy.json"),
            "types: file 'pharmacy.json' is not a known business type",
        ),
        (
            lambda g: (g / "types" / "veterinary.json").unlink(),
            "every health type has a type file: missing types/veterinary.json",
        ),
        (
            lambda g: (g / "businesses" / "fx-shop.json").rename(g / "businesses" / "FX Shop.json"),
            "businesses: file 'FX Shop.json' is not named after a business id",
        ),
        (
            lambda g: [p.unlink() for p in (g / "businesses").iterdir()],
            "the guard configuration folder holds no business",
        ),
    ],
)
def test_stray_missing_or_misnamed_entries_are_refused(
    tmp_path: Path, change: Callable[[Path], object], message: str
) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    change(guard)

    assert refused(guard, corpus) == message


@pytest.mark.parametrize("name", ["types", "businesses"])
def test_a_missing_subfolder_is_refused(tmp_path: Path, name: str) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    for path in (guard / name).iterdir():
        path.unlink()
    (guard / name).rmdir()

    assert refused(guard, corpus) == f"the guard configuration folder has no {name} folder"


def test_an_oversized_unreadable_or_non_utf8_file_is_refused(tmp_path: Path) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    path = guard / "businesses" / "fx-shop.json"
    path.write_bytes(b" " * (MAX_FILE_BYTES + 1))
    assert (
        refused(guard, corpus) == f"businesses/fx-shop.json is larger than {MAX_FILE_BYTES} bytes"
    )

    path.write_bytes(b"\xff\xfe")
    assert refused(guard, corpus) == "businesses/fx-shop.json is not UTF-8"

    path.unlink()
    path.mkdir()  # a folder where a file goes cannot be read
    assert refused(guard, corpus) == "unexpected entry 'fx-shop.json' in businesses"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_file_is_refused(tmp_path: Path) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    path = guard / "platform.json"
    path.chmod(0)
    try:
        assert refused(guard, corpus) == "platform.json cannot be read"
    finally:
        path.chmod(0o644)


def test_invalid_json_names_the_file_only(tmp_path: Path) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    (guard / "platform.json").write_text('{"red_flags": [' + MARKER, encoding="utf-8")

    assert refused(guard, corpus) == "platform.json is not a valid guard file: (file)"


VALID = "is not a valid guard file"
PLATFORM = "platform.json"
HUMAN = "types/human_healthcare.json"
VET = "types/veterinary.json"
CLINIC = "businesses/fx-clinic.json"
VET_FILE = "businesses/fx-vet.json"
SHOP = "businesses/fx-shop.json"


def appended(key: str, item: object) -> Callable[[dict[str, Any]], object]:
    return lambda d: d.setdefault(key, []).append(item)


def first(key: str, **fields: Any) -> Callable[[dict[str, Any]], object]:
    return lambda d: d[key][0].update(fields)


def without(key: str) -> Callable[[dict[str, Any]], object]:
    return lambda d: d.pop(key)


INVALID: list[tuple[str, Callable[[dict[str, Any]], object], str]] = [
    # Unknown type, category or intent (G4).
    (
        HUMAN,
        first("red_flags", intent="complaint"),
        f"{HUMAN} {VALID}: red_flags.0 (a red-flag"
        " term's intent is emergency or professional_judgement)",
    ),
    (HUMAN, first("red_flags", intent=MARKER), f"{HUMAN} {VALID}: red_flags.0.intent"),
    (PLATFORM, first("output", category=MARKER), f"{PLATFORM} {VALID}: output.0.category"),
    (
        PLATFORM,
        first("output", category="emergency"),
        f"{PLATFORM} {VALID}: output.0 (an output"
        " term's category is success_claim, medical_advice, diagnosis or abuse)",
    ),
    (
        VET,
        lambda d: d.update(business_type="human_healthcare"),
        f"{VET} names a different business_type than its filename",
    ),
    (VET, lambda d: d.update(business_type=MARKER), f"{VET} {VALID}: business_type"),
    (VET, without("not_covered_categories"), f"{VET} {VALID}: not_covered_categories"),
    (
        VET,
        lambda d: d.update(not_covered_categories=[MARKER]),
        f"{VET} {VALID}: not_covered_categories.0",
    ),
    (
        VET,
        lambda d: d.update(not_covered_categories=["self_harm", "self_harm"]),
        f"{VET} {VALID}: (file) (each not_covered category is listed once)",
    ),
    # A loosening business file: a removal key, a redefined term (G2, G4).
    (CLINIC, lambda d: d.update(remove=["fx_chest_pain"]), f"{CLINIC} {VALID}: {UNKNOWN_KEY}"),
    (CLINIC, lambda d: d.update({f"{MARKER} pain": False}), f"{CLINIC} {VALID}: {UNKNOWN_KEY}"),
    (CLINIC, first("red_flags", **{MARKER: 1}), f"{CLINIC} {VALID}: red_flags.0.{UNKNOWN_KEY}"),
    (
        CLINIC,
        appended("red_flags", red_flag(id="fx_distress_now", intent="professional_judgement")),
        f"{CLINIC}: red_flags.1 repeats the term id of {PLATFORM}: red_flags.0 (fx-clinic's terms):"
        " a file cannot redefine a term",
    ),
    (
        CLINIC,
        appended("red_flags", red_flag(id="fx_fainted_twice")),
        f"{CLINIC}: red_flags.1"
        f" repeats the term id of {CLINIC}: red_flags.0 (fx-clinic's terms): a file cannot redefine"
        " a term",
    ),
    (
        VET,
        appended("red_flags", red_flag(id="fx_chest_pain")),
        f"{VET}: red_flags.2 repeats the"
        f" term id of {HUMAN}: red_flags.0 (the platform's and every type's terms): a file cannot"
        " redefine a term",
    ),
    # The business file's own rules.
    (
        SHOP,
        lambda d: d.update(business_id="fx-vet"),
        f"{SHOP} names a different business_id than its filename",
    ),
    (
        SHOP,
        lambda d: d.update(synthetic=False),
        f"{SHOP} {VALID}: (file) (a guard file carries synthetic: true while Q4c is open)",
    ),
    (SHOP, without("synthetic"), f"{SHOP} {VALID}: synthetic"),
    (SHOP, lambda d: d.update(synthetic="yes"), f"{SHOP} {VALID}: synthetic"),
    # Every guard file carries synthetic: true while Q4c is open (G4; M7 PR 4).
    (
        PLATFORM,
        lambda d: d.update(synthetic=False),
        f"{PLATFORM} {VALID}: (file) (a guard file carries synthetic: true while Q4c is open)",
    ),
    (PLATFORM, without("synthetic"), f"{PLATFORM} {VALID}: synthetic"),
    (
        HUMAN,
        lambda d: d.update(synthetic=False),
        f"{HUMAN} {VALID}: (file) (a guard file carries synthetic: true while Q4c is open)",
    ),
    (VET, without("synthetic"), f"{VET} {VALID}: synthetic"),
    # The review note is one short line (M7 PR 4).
    (SHOP, lambda d: d.update(review="x" * 201), f"{SHOP} {VALID}: review"),
    (PLATFORM, lambda d: d.update(review=["not a line"]), f"{PLATFORM} {VALID}: review"),
    (VET_FILE, without("guidance"), f"{VET_FILE}: a health business has guidance lines (G3, G4)"),
    (
        VET_FILE,
        lambda d: d.update(guidance=[f"{MARKER}\U000e0041"]),
        f"{VET_FILE} {VALID}:"
        " guidance.0 (text fields are one trimmed line without control or invisible characters)",
    ),
    (VET_FILE, lambda d: d.update(guidance=["x" * 301]), f"{VET_FILE} {VALID}: guidance.0"),
    # Languages: covered or not_covered, and not_covered needs a script range (D3 (i)).
    (
        VET_FILE,
        without("not_covered"),
        f"{VET_FILE}: supported language 'te' has no injection,"
        " emergency, professional_judgement term; add terms or list it under not_covered",
    ),
    (
        VET_FILE,
        lambda d: d.update(not_covered=["te", "ta"]),
        f"{VET_FILE}: not_covered lists 'ta', which its corpus manifest does not support",
    ),
    (
        VET_FILE,
        lambda d: d.update(not_covered=["te", "te"]),
        f"{VET_FILE} {VALID}: (file) (each not_covered language is listed once)",
    ),
    (
        PLATFORM,
        lambda d: d.update(scripts=d["scripts"][:1]),
        f"{VET_FILE}: not_covered lists 'te',"
        " which has no script range in platform.json (D3 (i) cannot detect it)",
    ),
    (
        PLATFORM,
        lambda d: d["scripts"].append(d["scripts"][0]),
        f"{PLATFORM} {VALID}: (file) (each language's script is listed once)",
    ),
    (
        PLATFORM,
        lambda d: d["scripts"][0]["ranges"][0].update(first="U+0980"),
        f"{PLATFORM} {VALID}: scripts.0.ranges, scripts.0.ranges.0 (a range's first code point is"
        " at most its last, at most U+10FFFF)",
    ),
    (
        PLATFORM,
        lambda d: d["scripts"][0]["ranges"][0].update(last="U+110000"),
        f"{PLATFORM} {VALID}: scripts.0.ranges, scripts.0.ranges.0 (a range's first code point is"
        " at most its last, at most U+10FFFF)",
    ),
    (
        PLATFORM,
        lambda d: d["scripts"][0]["ranges"][0].update(first="0900"),
        f"{PLATFORM} {VALID}: scripts.0.ranges, scripts.0.ranges.0.first",
    ),
    (
        HUMAN,
        lambda d: d.update(red_flags=[t for t in d["red_flags"] if t["language"] == "en"]),
        f"{CLINIC}: supported language 'hi' has no professional_judgement term; add terms or list"
        " it under not_covered",
    ),
    # A term's shape.
    (
        PLATFORM,
        appended("injection", term(text="!!! ...")),
        f"{PLATFORM} {VALID}: injection.2 (a phrase term folds to at least one word)",
    ),
    (
        PLATFORM,
        appended("injection", term(text=" ".join([MARKER] * 9))),
        f"{PLATFORM} {VALID}: injection.2 (a phrase term folds to at most 8 words)",
    ),
    (
        PLATFORM,
        appended("injection", term(text=MARKER * 12)),
        f"{PLATFORM} {VALID}: injection.2.text",
    ),
    (
        PLATFORM,
        appended("injection", term(text=f"{MARKER}\u202e words")),
        f"{PLATFORM} {VALID}:"
        " injection.2.text (text fields are one trimmed line without control or invisible"
        " characters)",
    ),
    (
        PLATFORM,
        appended("injection", term(sets=[[MARKER], ["b"]])),
        f"{PLATFORM} {VALID}: injection.2 (a phrase term has text, and no sets or window)",
    ),
    (
        PLATFORM,
        appended(
            "injection", term(kind="phrase", text=None, sets=[[MARKER], ["b"]], window=2)
        ),  # the kind decides the shape, not the text's absence (mutant g45)
        f"{PLATFORM} {VALID}: injection.2 (a phrase term has text, and no sets or window)",
    ),
    (
        PLATFORM,
        appended("injection", term(window=3)),  # mutant g46
        f"{PLATFORM} {VALID}: injection.2 (a phrase term has text, and no sets or window)",
    ),
    (
        PLATFORM,
        appended("injection", term(kind="near", text=None, sets=[[MARKER], ["b"]])),
        f"{PLATFORM} {VALID}: injection.2 (a near term has sets and a window, and no text)",
    ),
    (
        PLATFORM,
        appended("injection", term(kind="near", text=None, sets=[[MARKER]], window=2)),
        f"{PLATFORM} {VALID}: injection.2 (a near term has 2 to 4 word sets)",
    ),
    (
        PLATFORM,
        appended(
            "injection",
            term(kind="near", text=None, sets=[[MARKER], ["b"], ["c"], ["d"], ["e"]], window=5),
        ),  # the upper bound (mutant g38)
        f"{PLATFORM} {VALID}: injection.2 (a near term has 2 to 4 word sets)",
    ),
    (
        PLATFORM,
        appended("injection", term(kind="near", text=None, sets=[[MARKER], []], window=2)),
        f"{PLATFORM} {VALID}: injection.2 (a near term's word set has 1 to 12 words)",
    ),
    (
        PLATFORM,
        appended(
            "injection",
            term(
                kind="near",
                text=None,
                sets=[[MARKER, *(f"w{i}" for i in range(12))], ["b"]],
                window=2,
            ),
        ),  # 13 words: the upper bound (mutant g39)
        f"{PLATFORM} {VALID}: injection.2 (a near term's word set has 1 to 12 words)",
    ),
    (
        PLATFORM,
        appended(
            "injection", term(kind="near", text=None, sets=[[f"{MARKER} b"], ["c"]], window=2)
        ),
        f"{PLATFORM} {VALID}: injection.2 (each word of a near term folds to exactly one word)",
    ),
    (
        PLATFORM,
        appended(
            "injection", term(kind="near", text=None, sets=[[MARKER], [MARKER.upper()]], window=2)
        ),
        f"{PLATFORM} {VALID}: injection.2 (a word belongs to one set of a near term, once)",
    ),
    (
        PLATFORM,
        appended("injection", term(kind="near", text=None, sets=[["a"], ["b"], ["c"]], window=2)),
        f"{PLATFORM} {VALID}: injection.2 (a near term's window holds at least one word per set)",
    ),
    (
        PLATFORM,
        appended("injection", term(kind="near", text=None, sets=[["a"], ["b"]], window=13)),
        f"{PLATFORM} {VALID}: injection.2.window",
    ),
    (
        PLATFORM,
        appended("injection", term(kind="near", text=None, sets=[["a"], ["b"]], window="4")),
        f"{PLATFORM} {VALID}: injection.2.window",
    ),
    (PLATFORM, appended("injection", term(kind=MARKER)), f"{PLATFORM} {VALID}: injection.2.kind"),
    (PLATFORM, appended("injection", term(id="Bad Id")), f"{PLATFORM} {VALID}: injection.2.id"),
    (
        PLATFORM,
        appended("injection", term(language=MARKER.upper())),
        f"{PLATFORM} {VALID}: injection.2.language",
    ),
    # Sources (G4): exactly one form; a red flag cites a line or a synonym rule.
    (
        HUMAN,
        first("red_flags", source={"rule": "made up"}),
        f"{HUMAN} {VALID}: red_flags.0 (a"
        " red-flag term cites a section line or a synonym rule (G4))",
    ),
    (
        HUMAN,
        first("red_flags", source={"section": "4.1"}),
        f"{HUMAN} {VALID}: red_flags.0.source (a section source has both section and item)",
    ),
    (
        HUMAN,
        first("red_flags", source={"section": "5", "item": "x"}),
        f"{HUMAN} {VALID}: red_flags.0.source.section",
    ),
    (
        HUMAN,
        first("red_flags", source={"synonym_of": "fx_distress_now"}),
        f"{HUMAN} {VALID}: red_flags.0.source (a synonym source states its rule)",
    ),
    (
        HUMAN,
        first("red_flags", source={"section": "4.1", "item": "x", "rule": "y"}),
        f"{HUMAN} {VALID}: red_flags.0.source (a source is exactly one of: section and item,"
        " synonym_of and rule, or rule)",
    ),
    (
        HUMAN,
        first("red_flags", source={}),
        f"{HUMAN} {VALID}: red_flags.0.source (a source is"
        " exactly one of: section and item, synonym_of and rule, or rule)",
    ),
    (
        HUMAN,
        lambda d: d["red_flags"][1]["source"].update(synonym_of="fx_no_such_term"),
        f"{HUMAN}: red_flags.1: its synonym_of names no other term of fx-clinic's terms",
    ),
    (
        HUMAN,
        lambda d: d["red_flags"][1]["source"].update(synonym_of="fx_chest_near"),
        f"{HUMAN}: red_flags.1: its synonym_of names no other term of fx-clinic's terms",
    ),
]


@pytest.mark.parametrize(("file", "change", "message"), INVALID)
def test_each_invalid_configuration_is_refused_naming_file_and_location(
    tmp_path: Path, file: str, change: Callable[[dict[str, Any]], object], message: str
) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    edit_json(guard / file, change)

    assert refused(guard, corpus) == message


# ── Refusals: the corpus manifest ────────────────────────────────────────────────────────────

MANIFEST = "fx-shop/manifest.json"


def test_a_business_without_a_corpus_manifest_is_refused(tmp_path: Path) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    (corpus / MANIFEST).unlink()

    assert refused(guard, corpus) == (
        f"{SHOP}: {MANIFEST}: no corpus manifest in the corpus folder (VECTAPILOT_CORPUS_DIR)"
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda d: d.update(business_type="pharmacy"),
            f"{SHOP}: {MANIFEST}: its business_type is not a known type",
        ),
        (lambda d: d.update(business_id="fx-vet"), f"{SHOP}: {MANIFEST} names another business"),
        (
            lambda d: d.update(languages=[]),
            f"{SHOP}: {MANIFEST} is not a valid corpus manifest: languages",
        ),
    ],
)
def test_an_invalid_or_foreign_manifest_is_refused(
    tmp_path: Path, change: Callable[[dict[str, Any]], object], message: str
) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    edit_json(corpus / MANIFEST, change)

    assert refused(guard, corpus) == message


def test_an_oversized_or_non_utf8_manifest_is_refused(tmp_path: Path) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    (corpus / MANIFEST).write_bytes(b"\xff\xfe")
    assert refused(guard, corpus) == f"{MANIFEST} is not UTF-8"

    (corpus / MANIFEST).write_bytes(b" " * (256 * 1024 + 1))
    assert "is larger than" in refused(guard, corpus)


def test_a_manifest_language_added_without_terms_is_refused(tmp_path: Path) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    edit_json(corpus / MANIFEST, lambda d: d.update(languages=["en", "hi"]))

    # The platform holds Hindi emergency and injection terms, which is all a non-health business
    # needs: covered. Remove the platform's Hindi injection term and it is not.
    assert isinstance(load_guard_dir(guard, corpus), GuardConfig)
    edit_json(
        guard / PLATFORM,
        lambda d: d.update(injection=[t for t in d["injection"] if t["language"] == "en"]),
    )
    assert refused(guard, corpus) == (
        f"{CLINIC}: supported language 'hi' has no injection term; add terms or list it under"
        " not_covered"
    )


def test_the_fixture_files_are_synthetic_and_name_no_sample_business() -> None:
    for path in sorted((GUARD_DIR / "businesses").glob("*.json")):
        data = read_json(path)
        assert data["synthetic"] is True
        assert data["business_id"].startswith("fx-")
    for path in [GUARD_DIR / PLATFORM, *sorted((GUARD_DIR / "types").glob("*.json"))]:
        assert read_json(path)["synthetic"] is True


def test_a_review_note_is_optional_and_kept_out_of_the_terms(tmp_path: Path) -> None:
    guard, corpus = copy_fixtures(tmp_path)
    before = load_guard_dir(guard, corpus)
    edit_json(guard / PLATFORM, lambda d: d.update(review="not professionally reviewed (Q8)"))
    after = load_guard_dir(guard, corpus)

    assert [t.term for t in after.terms] == [t.term for t in before.terms]
    assert after.rules_version != before.rules_version  # the file's bytes changed

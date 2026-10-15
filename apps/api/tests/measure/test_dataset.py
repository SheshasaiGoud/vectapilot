"""PR 8: the gateway_smoke dataset (evals/datasets/gateway_smoke/v0.1) and its loaders.

The dataset is synthetic (written by Claude for M1); these tests check its shape, not its wording.
shesha reviews the hi/te and code-mixed prompts before the live run (manifest `review`).
"""

import json
from collections import Counter
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.measure.dataset import (
    DatasetError,
    DatasetManifest,
    SmokeCase,
    check_dataset,
    load_cases,
    load_manifest,
)
from vectapilot_api.measure.schemas import SCHEMAS, BillCheck
from vectapilot_api.measure.summary import check_distinct_prompts

DATASET: Final = Path(__file__).parents[4] / "evals" / "datasets" / "gateway_smoke" / "v0.1"
MANIFEST: Final = load_manifest((DATASET / "manifest.json").read_text("utf-8"))
CASES: Final = load_cases((DATASET / "cases.jsonl").read_text("utf-8"))
ALIASES: Final = tuple(load_catalog().aliases)
CANARY: Final = "canary-prompt-7f3a"

# docs/business-context.md §7: the three fictional businesses and the languages each one lists.
SECTION_7_BUSINESSES: Final = {
    "sunrise-clinic": ("en", "hi"),
    "paws-and-co": ("en", "te"),
    "northwind-shop": ("en",),
}


# ── The v0.1 files ──────────────────────────────────────────────────────────────────────────


def test_the_manifest_says_what_the_dataset_is() -> None:
    assert (MANIFEST.dataset, MANIFEST.version, MANIFEST.synthetic) == (
        "gateway_smoke",
        "v0.1",
        True,
    )
    assert MANIFEST.businesses == SECTION_7_BUSINESSES


def test_every_case_parses_and_ids_and_prompts_are_unique() -> None:
    assert len(CASES) == 24  # "about 24 distinct prompts" (M1 plan, row 8)
    assert len({c.id for c in CASES}) == 24
    assert len({c.prompt for c in CASES}) == 24
    check_distinct_prompts(CASES)  # what the summary enforces: one prompt counts once toward n


def test_the_cases_agree_with_the_manifest_the_catalog_and_the_schemas() -> None:
    check_dataset(MANIFEST, CASES, aliases=ALIASES, schemas=SCHEMAS)  # raises on any problem


def test_businesses_languages_and_aliases_are_as_listed() -> None:
    assert {c.business_id for c in CASES} == set(SECTION_7_BUSINESSES)
    for case in CASES:
        assert case.language in SECTION_7_BUSINESSES[case.business_id], case.id
    assert Counter(c.language_group for c in CASES) == {
        "en": 14,
        "hi": 3,
        "hi-code-mixed": 2,
        "te": 3,
        "te-code-mixed": 2,
    }
    assert Counter(c.alias for c in CASES) == {"small": 12, "large": 12}


def test_no_language_is_tied_to_one_model() -> None:
    # Step 8 review: per-language figures are per alias (one model each), so every non-English
    # group is on both aliases, and hi and te have a plain case (tokens per character) on each.
    on = {(c.language_group, c.alias, c.kind) for c in CASES}
    for group in ("hi", "te", "hi-code-mixed", "te-code-mixed"):
        assert {alias for g, alias, _ in on if g == group} == {"small", "large"}, group
    for group in ("hi", "te"):
        assert {(group, "small", "plain"), (group, "large", "plain")} <= on, group


def test_exactly_one_forced_repair() -> None:
    [forced] = [c for c in CASES if c.forced_repair]

    assert (forced.id, forced.kind, forced.schema_name) == (
        "northwind-en-bill-forced-repair",
        "structured",
        "BillCheck",
    )


def test_each_alias_has_non_forced_structured_cases() -> None:
    # So first-try validity has an n on both aliases (five each).
    structured = Counter(c.alias for c in CASES if c.kind == "structured" and not c.forced_repair)
    assert structured == {"small": 5, "large": 5}


def test_every_schema_name_exists() -> None:
    names = {c.schema_name for c in CASES if c.kind == "structured"}
    assert names <= set(SCHEMAS)
    assert names == {"IntentLabel", "BookingRequest", "OrderQuery", "BillCheck"}


def test_there_is_a_long_non_english_answer_case() -> None:
    long_cases = {c.id: (c.language, c.alias) for c in CASES if c.id.endswith("long-answer")}
    assert long_cases == {
        "sunrise-hi-long-answer": ("hi", "large"),
        "paws-te-long-answer": ("te", "small"),
    }


def test_code_mixed_prompts_are_in_latin_script_and_native_ones_are_not() -> None:
    for case in CASES:
        if case.language == "en" or case.code_mixed:
            assert case.prompt.isascii() or "₹" in case.prompt, case.id
        else:
            assert not case.prompt.isascii(), case.id  # Devanagari or Telugu script


# ── The schemas and the forced repair ───────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_every_schema_is_closed(name: str) -> None:
    assert SCHEMAS[name].model_json_schema()["additionalProperties"] is False


def test_the_forced_repairs_rule_is_invisible_in_its_json_schema() -> None:
    # The model sees only this JSON Schema; the rule (total = sum of quantity x unit price) is
    # nowhere in it, so the first answer is expected to fail.
    text = json.dumps(BillCheck.model_json_schema()).lower()
    for word in ("sum", "equal", "match", "quantity x", "multiply"):
        assert word not in text


def test_the_forced_repairs_canned_replies_fail_then_pass() -> None:
    [forced] = [c for c in CASES if c.forced_repair]
    assert forced.fake_repair_reply is not None

    with pytest.raises(ValidationError) as caught:
        BillCheck.model_validate_json(forced.fake_reply)  # total 180; the items sum to 159

    [error] = caught.value.errors()
    assert (error["loc"], error["type"]) == (("total_inr",), "value_error")
    assert BillCheck.model_validate_json(forced.fake_repair_reply).total_inr == 159


def test_the_bill_rule_is_skipped_when_the_items_are_invalid() -> None:
    # Only the items' own error is reported, not a second one about the total.
    with pytest.raises(ValidationError) as caught:
        BillCheck.model_validate({"items": [], "total_inr": 5})

    assert [e["loc"] for e in caught.value.errors()] == [("items",)]


# ── The loaders and the rules they enforce ──────────────────────────────────────────────────


def case_fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "id": "test-case",
        "business_id": "northwind-shop",
        "alias": "small",
        "language": "en",
        "kind": "plain",
        "prompt": CANARY,
        "fake_reply": "ok",
    }
    return fields | overrides


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "structured"},  # no schema_name
        {"schema_name": "IntentLabel"},  # a plain case with a schema
        {"forced_repair": True, "fake_repair_reply": "ok"},  # forced, but plain
        {
            "kind": "structured",
            "schema_name": "BillCheck",
            "forced_repair": True,
        },  # no repair reply
        {"fake_repair_reply": "ok"},  # a repair reply without a forced repair
        {"code_mixed": True},  # en cannot be code-mixed
        {"language": "fr"},
        {"business_id": "Northwind"},
        {"unknown": 1},
    ],
)
def test_an_invalid_case_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SmokeCase.model_validate(case_fields(**overrides))


def test_load_cases_names_the_line_never_the_prompt() -> None:
    good = json.dumps(case_fields())
    bad = json.dumps(case_fields(id="other", language="fr"))

    with pytest.raises(DatasetError) as caught:
        load_cases(f"{good}\n\n{bad}\n")

    assert str(caught.value) == "cases.jsonl line 3 is not a valid case: language"
    assert CANARY not in str(caught.value)


def test_load_cases_refuses_duplicate_ids_and_an_empty_file() -> None:
    line = json.dumps(case_fields())

    with pytest.raises(DatasetError, match="unique; repeated: test-case"):
        load_cases(f"{line}\n{line}\n")
    with pytest.raises(DatasetError, match="no cases"):
        load_cases("\n")


def test_load_manifest_refuses_a_non_synthetic_dataset() -> None:
    fields = json.loads((DATASET / "manifest.json").read_text("utf-8")) | {"synthetic": False}

    with pytest.raises(DatasetError, match="not a valid manifest"):
        load_manifest(json.dumps(fields))


def manifest(**businesses: tuple[str, ...]) -> DatasetManifest:
    return MANIFEST.model_copy(update={"businesses": businesses or {"northwind-shop": ("en",)}})


@pytest.mark.parametrize(
    ("case", "problem"),
    [
        (case_fields(business_id="sunrise-clinic"), "its business is not in the manifest"),
        (case_fields(language="hi"), "its language is not one its business supports"),
        (case_fields(alias="medium"), "its alias is not in the catalog"),
        (
            case_fields(kind="structured", schema_name="Nope"),
            "its schema_name is not a known schema",
        ),
        (
            case_fields(kind="structured", schema_name="IntentLabel", fake_reply="{}"),
            "its fake_reply must validate",
        ),
        (
            case_fields(
                kind="structured",
                schema_name="BillCheck",
                forced_repair=True,
                fake_reply='{"items": [{"name": "pen", "quantity": 1, "unit_price_inr": 5}],'
                ' "total_inr": 5}',  # valid: a forced repair's first reply must not be
                fake_repair_reply="{}",
            ),
            "its fake_reply must validate, unless it is the forced repair's",
        ),
        (
            case_fields(
                kind="structured",
                schema_name="BillCheck",
                forced_repair=True,
                fake_reply="{}",
                fake_repair_reply="{}",
            ),
            "its fake_repair_reply must validate",
        ),
    ],
)
def test_check_dataset_names_the_case_and_the_problem(case: dict[str, Any], problem: str) -> None:
    with pytest.raises(DatasetError) as caught:
        check_dataset(
            manifest(), [SmokeCase.model_validate(case)], aliases=ALIASES, schemas=SCHEMAS
        )

    assert str(caught.value).startswith(f"case test-case: {problem}")

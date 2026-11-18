"""M7 PR 4: the real term files in `data/safety/` (synthetic, agent-drafted, not professionally
reviewed: Q8). shesha reviews every term (`docs/notes/m7-term-review.md`).

What is checked here, so a later edit cannot quietly drop it:
- the folder loads for the sample businesses as `create_app` loads it, and G4's startup refusals
  hold on the real files (each mutation below is refused, naming the file, never a term);
- every guidance line is verbatim from the business's own `emergency.md` and equals its
  `tools.json` guidance (decided 2026-11-16: this test, not the loader);
- every term cites a business-context line that is really there, or a synonym rule whose chain
  ends at one, or (injection and output only) a stated authoring rule; no source cites a frozen
  red-team case (the review rule: no term's only source is a frozen case);
- the coverage the plan decided: Sunrise in en, hi and romanised hi; Paws & Co in en; `te`
  `not_covered`; `self_harm` not covered by each health type; a mild `abuse` category (D2 (i));
- G14's self-test runs on every real term with the answer-key stand-in (plumbing only). With H1,
  in `test_match.py` behind H1's switch.
"""

import re
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import FastAPI

from tests.conftest import make_settings
from tests.guards.helpers import (
    MARKER,
    REPO_DIR,
    SAFETY_DIR,
    SAMPLE_CORPUS_DIR,
    SAMPLE_EXTRACTION_DIR,
    SANDBOX_DIR,
    AnswerKeyMatcher,
    edit_json,
    load_safety,
    read_json,
    sample_served,
    use_answer_key_selftest,
)
from tests.tools.test_sandbox_files import is_verbatim, units
from vectapilot_api.guards.config import (
    ConfiguredTerm,
    GuardConfig,
    GuardConfigError,
    NotCoveredCategory,
    load_guard_dir,
)
from vectapilot_api.guards.selftest import SelftestStatus, run_selftest
from vectapilot_api.guards.types import NearTerm, TermCategory
from vectapilot_api.intents.policy import BusinessType
from vectapilot_api.main import create_app

BUSINESS_CONTEXT: Final = REPO_DIR / "docs" / "business-context.md"
REVIEW_SHEET: Final = REPO_DIR / "docs" / "notes" / "m7-term-review.md"
SAMPLE_BUSINESSES: Final = ("paws-and-co", "sunrise-clinic")
Q8: Final = "not professionally reviewed (Q8)"
RULE_LABELS: Final = ("G7: ", "G8: ", "D2 (i): ")
"""The authoring rules an injection or output term may cite alone (the plan's G7, G8 and D2 (i))."""

PLATFORM: Final = "platform.json"
HUMAN: Final = "types/human_healthcare.json"
VET: Final = "types/veterinary.json"
SUNRISE: Final = "businesses/sunrise-clinic.json"
PAWS: Final = "businesses/paws-and-co.json"
FILES: Final = (PLATFORM, HUMAN, VET, PAWS, SUNRISE)

CONFIG: Final = load_safety()


def by_id() -> dict[str, ConfiguredTerm]:
    return {t.term.id: t for t in CONFIG.terms}


# ── It loads, as create_app loads it ─────────────────────────────────────────────────────────


def test_the_real_folder_loads_for_both_sample_businesses() -> None:
    assert tuple(CONFIG.businesses) == SAMPLE_BUSINESSES
    paws, sunrise = (CONFIG.for_business(b) for b in SAMPLE_BUSINESSES)

    assert (paws.business_type, sunrise.business_type) == (
        BusinessType.VETERINARY,
        BusinessType.HUMAN_HEALTHCARE,
    )
    assert paws.health and sunrise.health
    assert paws.not_covered == {"te"} and sunrise.not_covered == frozenset()
    assert len(CONFIG.rules_version) == 64
    assert CONFIG.not_covered_categories == {
        BusinessType.HUMAN_HEALTHCARE: frozenset({NotCoveredCategory.SELF_HARM}),
        BusinessType.VETERINARY: frozenset({NotCoveredCategory.SELF_HARM}),
    }
    # The unresolved set gets every type's terms (fail towards recall).
    assert len(CONFIG.unresolved.red_flags.terms) > len(sunrise.red_flags.terms)


def test_term_ids_are_unique_across_the_whole_folder() -> None:
    ids = [t.term.id for t in CONFIG.terms]

    assert len(ids) == len(set(ids))


def test_the_app_starts_with_every_folder_as_compose_sets_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # infra/compose.yaml's folders: the tool, extraction, corpus and guard folders together, so the
    # served types reach the guard loader. The self-test runs on the answer-key stand-in (helpers);
    # with H1 it runs in test_match.py.
    use_answer_key_selftest(monkeypatch)
    app: FastAPI = create_app(
        make_settings(
            tools_config_dir=SANDBOX_DIR,
            extraction_config_dir=SAMPLE_EXTRACTION_DIR,
            corpus_dir=SAMPLE_CORPUS_DIR,
            guard_config_dir=SAFETY_DIR,
        )
    )

    assert isinstance(app.state.guards, GuardConfig)
    assert app.state.guards.rules_version == CONFIG.rules_version
    assert app.state.guard_selftest.status is SelftestStatus.PASSED


def test_the_self_test_plumbing_runs_every_real_term_and_negative() -> None:
    # The answer-key stand-in is NOT H1: it proves every term yields its variants and every fixed
    # negative runs against every business's set. H1's own run is in test_match.py.
    result = run_selftest(CONFIG, AnswerKeyMatcher(CONFIG))

    assert result.status is SelftestStatus.PASSED
    assert result.terms == len(CONFIG.terms)
    assert result.negatives >= len(CONFIG.negatives) * (len(CONFIG.businesses) + 1)


# ── G4's startup refusals, on the real files ─────────────────────────────────────────────────


@pytest.fixture
def folders(tmp_path: Path) -> tuple[Path, Path]:
    """Throwaway copies: the real guard folder and the sample businesses' corpus manifests."""
    guard = shutil.copytree(SAFETY_DIR, tmp_path / "safety")
    corpus = tmp_path / "corpus"
    for business_id in SAMPLE_BUSINESSES:
        (corpus / business_id).mkdir(parents=True)
        shutil.copy(SAMPLE_CORPUS_DIR / business_id / "manifest.json", corpus / business_id)
    return guard, corpus


def _drop(key: str) -> Callable[[dict[str, Any]], object]:
    return lambda d: d.pop(key)


def _set(**fields: Any) -> Callable[[dict[str, Any]], object]:
    return lambda d: d.update(fields)


def _first_red_flag(**fields: Any) -> Callable[[dict[str, Any]], object]:
    return lambda d: d["red_flags"][0].update(fields)


def _repeat_platform_id(data: dict[str, Any]) -> None:
    platform = read_json(SAFETY_DIR / PLATFORM)
    data.setdefault("red_flags", []).append(platform["red_flags"][0])


REFUSALS: Final[list[tuple[str, Callable[[dict[str, Any]], object], str]]] = [
    (SUNRISE, _set(guidance=[]), f"{SUNRISE}: a health business has guidance lines (G3, G4)"),
    (SUNRISE, _set(business_id="paws-and-co"), "names a different business_id than its filename"),
    (SUNRISE, _set(synthetic=False), "synthetic: true while Q4c is open"),
    (PLATFORM, _set(synthetic=False), "synthetic: true while Q4c is open"),
    (HUMAN, _drop("synthetic"), f"{HUMAN} is not a valid guard file: synthetic"),
    (PAWS, _set(not_covered=[]), "supported language 'te' has no injection"),
    (VET, _drop("not_covered_categories"), f"{VET} is not a valid guard file"),
    (SUNRISE, _set(remove=["chest pain"]), "(unknown key)"),
    (SUNRISE, _repeat_platform_id, "a file cannot redefine a term"),
    (HUMAN, _first_red_flag(intent="question"), "emergency or professional_judgement"),
    (HUMAN, _first_red_flag(intent=MARKER), f"{HUMAN} is not a valid guard file: red_flags.0"),
    (VET, _first_red_flag(source={"rule": MARKER}), "cites a section line or a synonym rule"),
    (HUMAN, _set(business_type="ecommerce"), "names a different business_type than its filename"),
    (PLATFORM, _set(scripts=[]), "has no script range in platform.json"),
]


@pytest.mark.parametrize(("file", "change", "expected"), REFUSALS)
def test_startup_refuses_a_bad_change_to_the_real_files(
    folders: tuple[Path, Path], file: str, change: Callable[[dict[str, Any]], object], expected: str
) -> None:
    guard, corpus = folders
    assert isinstance(load_guard_dir(guard, corpus, served=sample_served()), GuardConfig)
    edit_json(guard / file, change)

    with pytest.raises(GuardConfigError) as error:
        load_guard_dir(guard, corpus, served=sample_served())

    message = str(error.value)
    assert expected in message
    assert MARKER not in message.lower()
    # Errors never name a term: no real term's text appears in the message.
    assert not any(
        t.spec.text and len(t.spec.text) > 6 and t.spec.text in message for t in CONFIG.terms
    )


def test_startup_refuses_the_real_folder_without_a_health_type_file(
    folders: tuple[Path, Path],
) -> None:
    guard, corpus = folders
    (guard / VET).unlink()

    with pytest.raises(GuardConfigError, match="every health type has a type file"):
        load_guard_dir(guard, corpus, served=sample_served())


def test_startup_refuses_a_served_health_business_without_a_file(
    folders: tuple[Path, Path],
) -> None:
    guard, corpus = folders
    (guard / PAWS).unlink()

    with pytest.raises(GuardConfigError, match="a guarded health business has no"):
        load_guard_dir(guard, corpus, served=sample_served())


# ── Synthetic and not professionally reviewed ────────────────────────────────────────────────


@pytest.mark.parametrize("file", FILES)
def test_every_file_is_synthetic_and_says_it_is_not_professionally_reviewed(file: str) -> None:
    data = read_json(SAFETY_DIR / file)

    assert data["synthetic"] is True
    assert Q8 in data["review"]


def test_the_folder_holds_exactly_the_planned_files() -> None:
    found = sorted(
        str(path.relative_to(SAFETY_DIR))
        for path in SAFETY_DIR.rglob("*")
        if path.is_file() and path.name != "README.md"
    )

    assert found == sorted(FILES)


# ── Guidance: verbatim from emergency.md, equal to tools.json ────────────────────────────────


@pytest.mark.parametrize("business_id", SAMPLE_BUSINESSES)
def test_guidance_is_verbatim_from_emergency_md_and_equals_tools_json(business_id: str) -> None:
    guidance = CONFIG.for_business(business_id).guidance
    emergency = (SAMPLE_CORPUS_DIR / business_id / "emergency.md").read_text(encoding="utf-8")
    tools = read_json(SANDBOX_DIR / business_id / "tools.json")

    assert guidance
    blocks = units(emergency)
    for line in guidance:
        assert is_verbatim(line, blocks), line
    assert list(guidance) == tools["emergency_guidance"]


# ── Sources: a real line, a synonym chain ending at one, or a stated rule; never a case ───────


def _sections() -> dict[str, str]:
    """business-context.md's sections 3.5, 4.1, 4.2 and 4.3, each as one whitespace-collapsed
    string (a table cell or a wrapped line still matches)."""
    found: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in [*BUSINESS_CONTEXT.read_text(encoding="utf-8").splitlines(), "#"]:
        if line.startswith("#"):
            if current is not None:
                found[current] = " ".join(" ".join(lines).split())
            heading = re.match(r"^### (\d\.\d) ", line)
            current = heading.group(1) if heading else None
            lines = []
        elif current is not None:
            lines.append(line)
    return found


SECTIONS: Final = _sections()

CASE_REFERENCE: Final = re.compile(
    r"\b[a-l]-\d{3}\b|red[- ]?team|cases\.jsonl|frozen|probe", re.IGNORECASE
)
"""What citing a frozen red-team case (or the probe) looks like in a source."""


def cites_a_case(text: str) -> bool:
    return CASE_REFERENCE.search(text) is not None


@pytest.mark.parametrize(
    "text",
    ["g-015", "as in redteam v0.1", "the red-team case h-005", "cases.jsonl line 4", "the probe"],
)
def test_red_proof_a_source_citing_a_case_is_caught(text: str) -> None:
    assert cites_a_case(text)


def test_every_section_item_is_really_in_its_section() -> None:
    assert set(SECTIONS) >= {"3.5", "4.1", "4.2", "4.3"}
    for configured in CONFIG.terms:
        source = configured.spec.source
        if source.section is not None:
            assert source.item is not None
            assert " ".join(source.item.split()) in SECTIONS[source.section], configured.where
            assert "self-harm" not in source.item, configured.where  # D6: no crisis terms


def test_no_source_cites_a_frozen_case_or_the_probe() -> None:
    for configured in CONFIG.terms:
        source = configured.spec.source
        for text in (source.item, source.rule):
            assert text is None or not cites_a_case(text), configured.where


def _chain(configured: ConfiguredTerm, terms: dict[str, ConfiguredTerm]) -> Iterator[str]:
    seen: set[str] = set()
    current = configured
    while current.spec.source.synonym_of is not None:
        assert current.term.id not in seen, f"{configured.where}: a synonym cycle"
        seen.add(current.term.id)
        current = terms[current.spec.source.synonym_of]
        yield current.term.id


def test_every_synonym_chain_ends_at_a_cited_line_or_a_stated_rule() -> None:
    terms = by_id()
    for configured in CONFIG.terms:
        chain = list(_chain(configured, terms))
        root = terms[chain[-1]] if chain else configured
        source = root.spec.source
        if configured.term.category in (
            TermCategory.EMERGENCY,
            TermCategory.PROFESSIONAL_JUDGEMENT,
        ):
            assert source.section is not None, configured.where
            assert root.term.category is configured.term.category, configured.where
        elif source.section is None:
            assert source.rule is not None
            assert source.rule.startswith(RULE_LABELS), configured.where


# ── Coverage, as decided ─────────────────────────────────────────────────────────────────────


def _kinds(business_id: str, category: TermCategory, language: str) -> set[str]:
    """`devanagari` and/or `latin` for the business's terms of one category and language."""
    guards = CONFIG.for_business(business_id)
    ids = {t.id for t in guards.all_terms().terms if t.category is category}
    scripts: set[str] = set()
    for configured in CONFIG.terms:
        if configured.term.id in ids and configured.spec.language == language:
            words = (
                configured.term.text
                if not isinstance(configured.term, NearTerm)
                else " ".join(w for words in configured.term.sets for w in words)
            )
            scripts.add("devanagari" if re.search("[ऀ-ॿ]", words) else "latin")
    return scripts


@pytest.mark.parametrize(
    ("business_id", "language", "scripts"),
    [
        ("sunrise-clinic", "en", {"latin"}),
        # hi holds Devanagari and romanised terms, matched as written (decided 2026-11-17).
        ("sunrise-clinic", "hi", {"devanagari", "latin"}),
        ("paws-and-co", "en", {"latin"}),
    ],
)
def test_each_business_language_has_emergency_judgement_and_injection_terms(
    business_id: str, language: str, scripts: set[str]
) -> None:
    for category in (
        TermCategory.EMERGENCY,
        TermCategory.PROFESSIONAL_JUDGEMENT,
        TermCategory.INJECTION,
    ):
        assert _kinds(business_id, category, language) >= scripts, category


def test_no_telugu_terms_while_te_has_no_reviewer() -> None:
    assert not [t.where for t in CONFIG.terms if t.spec.language == "te"]
    assert not [t.where for t in CONFIG.terms if re.search("[ఀ-౿]", t.term.id)]
    for configured in CONFIG.terms:
        text = configured.spec.text or " ".join(w for s in configured.spec.sets or () for w in s)
        assert not re.search("[ఀ-౿]", text), configured.where


def test_near_terms_cover_word_order_for_every_red_flag_language() -> None:
    for business_id, language in (
        ("sunrise-clinic", "en"),
        ("sunrise-clinic", "hi"),
        ("paws-and-co", "en"),
    ):
        red_flags = {t.id for t in CONFIG.for_business(business_id).red_flags.terms}
        nears = [
            t
            for t in CONFIG.terms
            if t.term.id in red_flags
            and t.spec.language == language
            and isinstance(t.term, NearTerm)
        ]
        assert nears, (business_id, language)


RED_FLAG_WINDOW_MIN: Final = 5
"""The red-team README's rule, stated before PR 4 (evals/datasets/redteam/v0.1/README.md):
word order needs a near window of at least 5 words (noah's PR 4 review)."""

WITHIN_N_WORDS: Final = re.compile(r"\bwithin (\d+) words\b")


def test_every_red_flag_near_term_has_a_window_of_at_least_five_words() -> None:
    narrow = [
        t.where
        for t in CONFIG.terms
        if isinstance(t.term, NearTerm)
        and t.term.category in (TermCategory.EMERGENCY, TermCategory.PROFESSIONAL_JUDGEMENT)
        and t.term.window < RED_FLAG_WINDOW_MIN
    ]

    assert not narrow


def test_a_source_saying_within_n_words_means_window_n() -> None:
    # One convention in the folder: "within N words" is `window: N` (H1's "within window
    # consecutive words"), so a rule cannot say five while the term says six.
    for configured in CONFIG.terms:
        rule = configured.spec.source.rule or ""
        for n in WITHIN_N_WORDS.findall(rule):
            assert isinstance(configured.term, NearTerm), configured.where
            assert int(n) == configured.term.window, configured.where


def test_output_categories_are_where_g8_puts_them() -> None:
    platform = read_json(SAFETY_DIR / PLATFORM)
    types = [read_json(SAFETY_DIR / f) for f in (HUMAN, VET)]
    platform_output = {t["category"] for t in platform["output"]}
    type_output = {t["category"] for data in types for t in data["output"]}

    # Success claims, medical advice and abuse on every type; diagnosis on health types only.
    assert platform_output == {"success_claim", "medical_advice", "abuse"}
    assert type_output == {"diagnosis"}
    # D2 (i): a small, mild abuse category in en and hi.
    abuse = [t for t in platform["output"] if t["category"] == "abuse"]
    assert {t["language"] for t in abuse} == {"en", "hi"}
    assert len(abuse) <= 20


def test_business_files_add_guidance_and_languages_only() -> None:
    for file in (SUNRISE, PAWS):
        data = read_json(SAFETY_DIR / file)
        assert not data.get("red_flags") and not data.get("injection") and not data.get("output")


# ── The review sheet ─────────────────────────────────────────────────────────────────────────


def test_the_review_sheet_lists_every_term_once() -> None:
    sheet = REVIEW_SHEET.read_text(encoding="utf-8")
    rows = re.findall(r"^\| \[[ x]\] \| `([a-z0-9_]+)` \|", sheet, flags=re.MULTILINE)

    assert sorted(rows) == sorted(t.term.id for t in CONFIG.terms)


def test_compose_mounts_the_folder_read_only_and_names_it() -> None:
    compose = (REPO_DIR / "infra" / "compose.yaml").read_text(encoding="utf-8")

    assert "VECTAPILOT_GUARD_CONFIG_DIR: /app/data/safety\n" in compose
    assert "- ../data/safety:/app/data/safety:ro\n" in compose

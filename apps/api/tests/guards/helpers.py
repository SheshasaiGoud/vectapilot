"""Shared fixtures for the guard tests: the fixture guard folder, its fixture corpus, and a matcher
stand-in that is NOT H1.

The fixture folder (`fixtures/guard/`) holds synthetic test terms for three fictional businesses
(`fx-clinic`, `fx-vet`, `fx-shop`); the real term files are `data/safety/` (PR 4,
`test_safety_terms.py`). Every fixture guidance line is invented and marked fictional.
"""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

from vectapilot_api import main
from vectapilot_api.guards.config import ConfiguredTerm, GuardConfig, load_guard_dir
from vectapilot_api.guards.selftest import SelftestResult, positives, run_selftest, screen_text
from vectapilot_api.guards.types import Folded, TermHit, TermSet
from vectapilot_api.intents.policy import BusinessType

REPO_DIR: Final = Path(__file__).resolve().parents[4]
SANDBOX_DIR: Final = REPO_DIR / "data" / "sandbox"
SAFETY_DIR: Final = REPO_DIR / "data" / "safety"
"""The real term files (M7 PR 4): synthetic, agent-drafted, not professionally reviewed (Q8)."""
SAMPLE_CORPUS_DIR: Final = REPO_DIR / "data" / "sample"
SAMPLE_EXTRACTION_DIR: Final = REPO_DIR / "data" / "extraction"
FIXTURES: Final = Path(__file__).resolve().parent / "fixtures"
GUARD_DIR: Final = FIXTURES / "guard"
CORPUS_DIR: Final = FIXTURES / "corpus"
BUSINESSES: Final = ("fx-clinic", "fx-shop", "fx-vet")

MARKER: Final = "zqxmarker"
"""Text a test writes into a file where a term or an unknown key would go: no error may echo it."""


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def edit_json(path: Path, change: Callable[[dict[str, Any]], object]) -> None:
    data = read_json(path)
    change(data)
    write_json(path, data)


def copy_fixtures(destination: Path) -> tuple[Path, Path]:
    """Throwaway copies of the fixture guard folder and corpus: `(guard, corpus)`."""
    guard = shutil.copytree(GUARD_DIR, destination / "guard")
    corpus = shutil.copytree(CORPUS_DIR, destination / "corpus")
    return guard, corpus


def sample_guard_dir(destination: Path) -> Path:
    """A guard folder for the two sample businesses (`data/sample/`), for tests that start the app
    with a tool or extraction folder outside local, test and ci: the fixture platform and type
    files, and per business its languages' `not_covered` and the guidance lines of its own
    `tools.json` (copied, never written here). The terms stay fixture terms; the real ones are
    `SAFETY_DIR`."""
    guard = shutil.copytree(GUARD_DIR, destination)
    for path in (guard / "businesses").iterdir():
        path.unlink()
    for business_id, not_covered in (("paws-and-co", ["te"]), ("sunrise-clinic", [])):
        tools = read_json(SANDBOX_DIR / business_id / "tools.json")
        write_json(
            guard / "businesses" / f"{business_id}.json",
            {
                "business_id": business_id,
                "synthetic": True,
                "not_covered": not_covered,
                "guidance": tools["emergency_guidance"],
            },
        )
    return guard


def load_fixtures() -> GuardConfig:
    return load_guard_dir(GUARD_DIR, CORPUS_DIR)


def sample_served() -> dict[str, BusinessType]:
    """What `create_app` passes as `served` for the sample businesses: each `tools.json`'s type."""
    return {
        path.name: BusinessType(read_json(path / "tools.json")["business_type"])
        for path in sorted(SANDBOX_DIR.iterdir())
        if (path / "tools.json").is_file()
    }


def load_safety() -> GuardConfig:
    """The real term files (`data/safety/`) for the sample corpus, as `create_app` loads them."""
    return load_guard_dir(SAFETY_DIR, SAMPLE_CORPUS_DIR, served=sample_served())


def configured(config: GuardConfig, term_id: str) -> ConfiguredTerm:
    return next(t for t in config.terms if t.term.id == term_id)


class AnswerKeyMatcher:
    """NOT H1, and not a matcher: a test stand-in that *knows the answers*. It hits a term exactly
    when the folded text is one of the self-test's own positives for that term, so it can only
    test the self-test's plumbing (positives must hit, negatives must not, errors are named). It
    says nothing about how a matcher should work; H1 is shesha's (D5 (b))."""

    def __init__(self, config: GuardConfig) -> None:
        self.calls = 0
        self.answers: dict[str, frozenset[str]] = {
            t.term.id: frozenset(screen_text(text) for _, text in positives(t))
            for t in config.terms
        }

    def __call__(self, text: Folded, terms: TermSet) -> tuple[TermHit, ...]:
        self.calls += 1
        return tuple(
            TermHit(term.id, term.category, 0)
            for term in terms.terms
            if text in self.answers.get(term.id, frozenset())
        )


def use_answer_key_selftest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `create_app`'s startup self-test (G14) run on `AnswerKeyMatcher`, not H1, for this test.

    `create_app` self-tests every guard folder through H1, so a half-written H1 would stop every
    app a test starts with a guard folder, far from H1's own tests (diego's PR 1 review). Tests
    about the log canary, the settings and the routes use this; the one startup test that runs H1
    is in test_match.py, behind H1's switch. `main` imports the name, so patching it there works.
    """

    def answer_key(config: GuardConfig) -> SelftestResult:
        return run_selftest(config, AnswerKeyMatcher(config))

    monkeypatch.setattr(main, "run_selftest", answer_key)

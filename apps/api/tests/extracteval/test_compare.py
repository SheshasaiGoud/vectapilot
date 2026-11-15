"""The eval's own comparison (`extracteval/compare.py`; plan, "Scoring rules", "Correct value").

Exact for dates, enums and identifiers (whitespace trimmed); for text, whitespace collapsed,
case-folded and punctuation at either end dropped (E2's own "quote minus its label and
punctuation"; amended 2026-11-15, ananya-rag's PR 4 review); both null is correct. And the rule
that makes it independent: neither the comparison nor H2's module imports
`vectapilot_api.extraction`, directly or through anything they import.
"""

import ast
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from vectapilot_api.extracteval import compare, metrics
from vectapilot_api.extracteval.compare import FIELD_KINDS, FieldKind, same_value

INDEPENDENT: Final = (compare, metrics)


@pytest.mark.parametrize(
    ("kind", "gold", "value", "expected"),
    [
        # Both null is correct; one null is not.
        ("text", None, None, True),
        ("date", None, None, True),
        ("text", None, "Asha Rao", False),
        ("identifier", "LHM-204117", None, False),
        # Exact kinds: surrounding whitespace trimmed, nothing else.
        ("date", "2026-03-03", "2026-03-03", True),
        ("date", "2026-03-03", " 2026-03-03\n", True),
        ("date", "2026-03-03", "3 March 2026", False),
        ("date", "2026-03-03", "2026-3-3", False),
        ("enum", "dog", "dog", True),
        ("enum", "dog", "Dog", False),
        ("enum", "dog", "Canine", False),
        ("identifier", "LHM-204117", "LHM-204117", True),
        ("identifier", "LHM-204117", "lhm-204117", False),
        ("identifier", "LHM-204117", "LHM 204117", False),
        ("identifier", "AB123", "AB1", False),
        ("identifier", "LHM-204117", "LHM\u2013204117", False),
        # Text: whitespace collapsed, case-folded, punctuation at either end dropped.
        ("text", "Dr Meera Iyer", "dr  meera\tIYER", True),
        ("text", "Dr Meera Iyer", " Dr Meera Iyer ", True),
        ("text", "Dr Meera Iyer", "Dr. Meera Iyer", False),
        ("text", "Asha Rao", "Asha", False),
        ("text", "Stra\u00dfe Clinic", "STRASSE clinic", True),
        ("text", "Paws and Co Clinic", "Paws & Co Clinic", False),
        # A final period is a punctuation habit, both ways (E2 accepts either quote).
        ("text", "Larkfield Mutual Ltd.", "Larkfield Mutual Ltd", True),
        ("text", "Larkfield Mutual Ltd", "Larkfield Mutual Ltd.", True),
        ("text", "Larkfield Mutual", "(Larkfield Mutual),", True),
        ("text", "Larkfield Mutual", "Larkfield Mutual .", True),
        # Inner punctuation still counts; only the ends are trimmed.
        ("text", "Dr. Tomas Brell", "Dr Tomas Brell", False),
        ("text", "Larkfield Mutual Ltd.", "Larkfield Mutual, Ltd", False),
        # A value that is only punctuation is never a name.
        ("text", "Larkfield Mutual", "...", False),
        # The exact kinds are not trimmed of punctuation.
        ("identifier", "LFM-204117", "LFM-204117.", False),
        ("enum", "dog", "dog.", False),
        ("date", "2026-03-03", "2026-03-03.", False),
    ],
)
def test_same_value(kind: FieldKind, gold: str | None, value: str | None, expected: bool) -> None:
    assert same_value(kind, gold, value) is expected


def test_trim_drops_only_what_is_not_a_letter_or_digit_at_either_end() -> None:
    assert compare.trim(" (Ltd.) ") == "Ltd"
    assert compare.trim("Dr. Brell") == "Dr. Brell"
    assert compare.trim("...") == ""
    assert compare.trim("") == ""
    assert compare.trim("\u00e9cole.") == "\u00e9cole"  # a letter is a letter in any script


def test_a_kind_outside_the_four_is_a_caller_bug() -> None:
    assert FIELD_KINDS == ("text", "date", "enum", "identifier")
    with pytest.raises(ValueError, match="kind is one of"):
        same_value("number", "1", "1")  # type: ignore[arg-type]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
    return found


@pytest.mark.parametrize("module", INDEPENDENT, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_the_comparison_and_h2_never_import_the_extraction_core(module: object) -> None:
    path = Path(str(getattr(module, "__file__", "")))
    imported = _imports(path)
    assert not {name for name in imported if name.startswith("vectapilot_api.extraction")}
    assert not {name for name in imported if name.startswith("vectapilot_api.extracteval.")}


def test_nor_does_anything_they_import() -> None:
    names = ", ".join(module.__name__ for module in INDEPENDENT)
    probe = (
        f"import sys; import {names}; "
        "bad = sorted(m for m in sys.modules if m == 'vectapilot_api.extraction'"
        " or m.startswith('vectapilot_api.extraction.')); print(bad); sys.exit(1 if bad else 0)"
    )
    done = subprocess.run(  # noqa: S603 (the test's own interpreter, a fixed probe)
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False, timeout=60
    )
    assert done.returncode == 0, done.stdout + done.stderr

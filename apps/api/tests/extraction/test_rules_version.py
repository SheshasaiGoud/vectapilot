"""M6 PR 2: `EXTRACTION_RULES_VERSION` moves with the rules (plan, "Scoring rules").

`signals.py`, `routing.py` and `invariants.py` decide levels and statuses. An eval run names the
rules version in its fingerprint, so a change to any of them without a new version fails here,
as a prompt change without a new `PROMPT_VERSION` does (tests/answer, tests/extraction/
test_prompt.py). To bump: set a new `EXTRACTION_RULES_VERSION` in `signals.py`, run this test,
and add the printed fingerprint to `rules_fingerprints.py` under the new version (keep old ones).
"""

import re
from pathlib import Path

from vectapilot_api.extraction import rules_fingerprints, signals
from vectapilot_api.extraction.rules_fingerprints import (
    EXTRACTION_RULES_FINGERPRINTS,
    RULES_FILES,
    rules_fingerprint,
)
from vectapilot_api.extraction.signals import EXTRACTION_RULES_VERSION


def test_the_current_rules_have_the_current_versions_fingerprint() -> None:
    found = rules_fingerprint()
    assert EXTRACTION_RULES_FINGERPRINTS.get(EXTRACTION_RULES_VERSION) == found, (
        "signals.py, routing.py or invariants.py changed: bump EXTRACTION_RULES_VERSION in"
        f" signals.py and add {found!r} under it in rules_fingerprints.py"
    )


def test_the_version_is_an_opaque_name_and_every_fingerprint_a_sha256() -> None:
    assert re.fullmatch(r"extraction-rules\.[1-9][0-9]*", EXTRACTION_RULES_VERSION)
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in EXTRACTION_RULES_FINGERPRINTS.values())


def test_the_fingerprint_covers_the_three_rules_files_and_not_its_own_map(tmp_path: Path) -> None:
    package = Path(signals.__file__).parent
    assert RULES_FILES == ("signals.py", "routing.py", "invariants.py")
    assert Path(rules_fingerprints.__file__).name not in RULES_FILES
    for name in RULES_FILES:
        (tmp_path / name).write_bytes((package / name).read_bytes())
    assert rules_fingerprint(tmp_path) == rules_fingerprint()
    for name in RULES_FILES:  # one changed byte in any of them moves it
        original = (tmp_path / name).read_bytes()
        (tmp_path / name).write_bytes(original + b"\n# changed\n")
        assert rules_fingerprint(tmp_path) != rules_fingerprint()
        (tmp_path / name).write_bytes(original)


def test_line_ends_do_not_move_the_fingerprint(tmp_path: Path) -> None:
    package = Path(signals.__file__).parent
    for name in RULES_FILES:
        (tmp_path / name).write_bytes((package / name).read_bytes().replace(b"\n", b"\r\n"))
    assert rules_fingerprint(tmp_path) == rules_fingerprint()

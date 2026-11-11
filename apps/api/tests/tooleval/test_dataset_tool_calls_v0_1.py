"""The committed `tool_calls` v0.1 (M5 PR 6): what CI checks on every change.

While collecting, every scenario must pass `check` on its own (zero scenarios pass). Once frozen,
the same test enforces D8's minimums, the split's pins and the sandbox pins. The decided values
are pinned here (D8's of 2026-11-10 and the emergency minimums of 2026-11-11): lowering a minimum,
or changing the split rule, is a decision, not an edit.
"""

import json
from typing import Final

from tests.tooleval.helpers import COMMITTED
from vectapilot_api.tooleval.checks import check_dataset
from vectapilot_api.tooleval.files import read_dataset
from vectapilot_api.tooleval.scenarios import MANIFEST_FILE

D8_MINIMUMS: Final = {
    "min_scenarios": 40,
    "min_per_business": 15,
    "min_per_language_group_per_business": 2,
    "min_confirm": 6,
    "min_steering": 6,
    "min_read_only": 6,
    "min_emergency": 4,
    # Decided 2026-11-11 (shesha, M5 PR 6 review): per business, and Sunrise in Hindi or Hinglish.
    "min_emergency_per_business": 2,
    "min_emergency_hindi_per_business": 1,
}


def test_the_committed_version_passes_its_check() -> None:
    result = check_dataset(read_dataset(COMMITTED))
    assert result.problems == ()


def test_the_manifest_holds_d8s_minimums_and_the_split_rule_fixed_before_any_scenario() -> None:
    manifest = json.loads((COMMITTED / MANIFEST_FILE).read_text("utf-8"))
    assert manifest["synthetic"] is True
    assert manifest["requirements"] == D8_MINIMUMS
    assert manifest["split_rule"] == {
        "rule": "stratified-systematic-v2",
        "seed": "tool_calls",
        "test_percent": 50,
    }
    assert manifest["near_duplicate"] == {"method": "char-3gram-jaccard", "threshold_percent": 70}
    assert manifest["businesses"] == ["sunrise-clinic", "paws-and-co"]


def test_no_agent_authored_scenario_and_no_example_in_the_readme() -> None:
    dataset = read_dataset(COMMITTED)
    assert {s.author for s in dataset.scenarios} <= {"shesha", "friend"}
    readme = (COMMITTED / "README.md").read_text("utf-8")
    assert "## Scoring rules" in readme
    assert "## Writers' brief" in readme
    # The brief shows no example scenario (no JSON line a writer could copy).
    assert '"message":' not in readme

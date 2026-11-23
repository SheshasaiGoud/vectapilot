"""The frozen red-team suite as the tests read it: `evals/datasets/redteam/v0.1/` (M7 PR 2).

Read once at import, through the same reader `redteam check` uses, so the parametrised
`test_case[<id>]` runs exactly the cases `freeze` pinned (`tests/test_redteam_gate.py` and the
gate script check that the run and the freeze agree).
"""

from pathlib import Path
from typing import Final

from vectapilot_api.redteam.cases import Case, Label
from vectapilot_api.redteam.checks import RedteamDataset, generator_of
from vectapilot_api.redteam.files import DEFAULT_DATASET, read_dataset
from vectapilot_api.redteam.graduated import scored_labels
from vectapilot_api.redteam.pii import PiiGenerator
from vectapilot_api.redteam.plan import end_to_end_ids

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
DATASET_DIR: Final = REPO_ROOT / DEFAULT_DATASET
DATASET: Final[RedteamDataset] = read_dataset(DATASET_DIR)
CASES: Final[dict[str, Case]] = {case.id: case for case in DATASET.cases}
END_TO_END: Final = end_to_end_ids(DATASET.cases)
GENERATOR: Final[PiiGenerator] = generator_of(DATASET)
"""The frozen seed's generator (the default seed while collecting)."""
LABELS: Final[dict[str, Label]] = scored_labels(DATASET.gates.labels, DATASET.manifest.version)
"""The labels the suite scores with: gates.json's, with every graduated known gap as hard
(`redteam.graduated`, decided 2026-11-21)."""


def label_of(case_id: str) -> Label:
    return LABELS[case_id]

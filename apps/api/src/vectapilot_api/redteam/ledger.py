"""M7's run ledger, `evals/reports/redteam/test-runs.jsonl` (scoring rules, "Dry or live; test
once"; carried from ADR 0013 decision 5).

**Once per config.** D7's live raw-arm run is ledgered once per fingerprint
(`runinfo.Fingerprint`), and the post-freeze probe once per rules version (the SHA-256 of
`data/safety/`, which the fingerprint holds); a second run of one config needs a
`--test-rerun-reason`, recorded here. The entry is M2's `LedgerEntry`, appended before the run's
first paid call (`evalrun/ledger.py`'s `append_ledger`: one JSON line, flushed at once) and
committed with the folder; the note reports the first ledgered run and lists every rerun.
"""

from collections.abc import Sequence
from typing import Final

from vectapilot_api.evalrun.ledger import LEDGER_FILE, LedgerEntry
from vectapilot_api.redteam.runinfo import REDTEAM_REPORTS

LEDGER_PATH: Final = REDTEAM_REPORTS / LEDGER_FILE
"""Relative to the repository root."""


def runs_of(
    entries: Sequence[LedgerEntry], dataset: str, version: str, freeze: str, fingerprint: str
) -> list[LedgerEntry]:
    """The ledgered runs of one config on one frozen version, in order (the first is the one the
    note reports)."""
    return [
        e
        for e in entries
        if (e.dataset, e.version, e.freeze_sha256, e.config_fingerprint)
        == (dataset, version, freeze, fingerprint)
    ]

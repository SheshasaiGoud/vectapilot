"""M5's test-run ledger, `evals/reports/tool_calls/test-runs.jsonl` (scoring rules, "Dry or real;
test once"; carried from ADR 0013 decision 5).

**Once per config.** Every M5 test run is live and paid, so M2's key holds here: a second test run
with the same fingerprint (`runinfo.Fingerprint`: the prompt version, the alias and its model,
`catalog_version`, each step schema, the sandbox and freeze hashes, the limits) needs
`--test-rerun-reason`, recorded in the ledger. The entry is M2's `LedgerEntry`, appended before
the run's first scenario (`evalrun/ledger.py`'s `append_ledger`: one JSON line, flushed at once)
and committed with the folder; the note reports the first ledgered run and lists every rerun.
"""

from collections.abc import Sequence
from typing import Final

from vectapilot_api.evalrun.ledger import LEDGER_FILE, LedgerEntry
from vectapilot_api.tooleval.runinfo import TOOL_REPORTS

LEDGER_PATH: Final = TOOL_REPORTS / LEDGER_FILE
"""Relative to the repository root."""


def runs_of(
    entries: Sequence[LedgerEntry], dataset: str, version: str, freeze: str, fingerprint: str
) -> list[LedgerEntry]:
    """The ledgered test runs of one config on one frozen version, in order (the first is the one
    the note reports)."""
    return [
        e
        for e in entries
        if (e.dataset, e.version, e.freeze_sha256, e.config_fingerprint)
        == (dataset, version, freeze, fingerprint)
    ]

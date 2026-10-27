"""The test-run ledger, `evals/reports/<dataset>/test-runs.jsonl` (ADR 0013 decision 5).

One line per test run, appended before its first paid call (`cli.run_command`), committed with the
test folder. `run` reads it to refuse a second test run of one config without a reason; `compare`
reads it as committed in HEAD to take only test folders already in it (M3 PR 6). Moved here from
`cli.py` in M3 PR 6 so both can read it; nothing about the format changed.
"""

from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

LEDGER_FILE: Final = "test-runs.jsonl"


class LedgerEntry(BaseModel):
    """One test run, appended before its first paid call
    (`evals/reports/<dataset>/test-runs.jsonl`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset: str
    version: str
    freeze_sha256: str | None
    config_fingerprint: str
    run_id: str
    commit: str
    started_at: datetime
    rerun_reason: str | None


def parse_ledger(text: str) -> list[LedgerEntry]:
    """Every entry of a ledger's text, in order (blank lines skipped)."""
    return [LedgerEntry.model_validate_json(line) for line in text.splitlines() if line.strip()]


def read_ledger(path: Path) -> list[LedgerEntry]:
    if not path.is_file():
        return []
    return parse_ledger(path.read_text("utf-8"))


def append_ledger(path: Path, entry: LedgerEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as ledger:
        ledger.write(entry.model_dump_json() + "\n")
        ledger.flush()

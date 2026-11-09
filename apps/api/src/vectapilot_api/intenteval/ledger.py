"""M4's test-run ledger, `evals/reports/intents/test-runs.jsonl` (plan, "Test discipline").

**Once per mode, not once per config.** M2/M3's ledger keys a test run by its config
fingerprint; classifier and hybrid runs cost $0, so that key would let a retrain call itself a
new config and peek at test for free. Here the once-only key is (dataset, version, freeze hash,
mode): a second test run of a mode needs `--test-rerun-reason`, and the note reports the first
ledgered run and lists every rerun.

**The pin comes first.** Before any test run, one `PinEntry` pins the artifact sha256, T, tau,
sigma and `ROUTER_PROMPT_VERSION` (with the model folder they came from), all committed in HEAD;
a classifier test run and a hybrid on test refuse any other artifact or threshold, and an LLM test
run refuses another prompt version. One pin per frozen version: a second is refused (a new
version is the way to a new pin).

The append discipline is `evalrun/ledger.py`'s (`append_ledger`, one JSON line flushed at once,
written before the run's first call and committed with its folder); the entries are M4's own.
"""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from vectapilot_api.evalrun.ledger import LEDGER_FILE
from vectapilot_api.evalset.corpus import Sha256
from vectapilot_api.intenteval.runinfo import INTENT_REPORTS

LEDGER_PATH: Final = INTENT_REPORTS / LEDGER_FILE
"""Relative to the repository root."""

LedgerMode = Literal["classifier", "llm"]
"""The modes with test runs; the hybrid is replayed from them (`summarize`), never run."""


class _Closed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class PinEntry(_Closed):
    """What every test run of this frozen version must use."""

    kind: Literal["pin"] = "pin"
    dataset: str
    version: str
    freeze_sha256: Sha256
    model_folder: str
    artifact_sha256: Sha256
    temperature: float
    tau: float | None
    """None: the pinned thresholds have no confidence band (decided 2026-11-09)."""
    sigma: float
    router_prompt_version: str
    commit: str
    pinned_at: datetime


class RunEntry(_Closed):
    """One test run, appended before its first call."""

    kind: Literal["run"] = "run"
    dataset: str
    version: str
    freeze_sha256: Sha256
    mode: LedgerMode
    fingerprint_sha256: Sha256
    run_id: str
    commit: str
    started_at: datetime
    rerun_reason: str | None


Entry = Annotated[PinEntry | RunEntry, Field(discriminator="kind")]
_ENTRY: Final = TypeAdapter[PinEntry | RunEntry](Entry)


def parse_entries(text: str) -> list[PinEntry | RunEntry]:
    """Every entry, in order (blank lines skipped). Raises pydantic's ValidationError."""
    return [_ENTRY.validate_json(line) for line in text.splitlines() if line.strip()]


def read_entries(path: Path) -> list[PinEntry | RunEntry]:
    return parse_entries(path.read_text("utf-8")) if path.is_file() else []


def pin_for(
    entries: Sequence[PinEntry | RunEntry], dataset: str, version: str, freeze: str
) -> PinEntry | None:
    pins = [
        e
        for e in entries
        if isinstance(e, PinEntry)
        and (e.dataset, e.version, e.freeze_sha256) == (dataset, version, freeze)
    ]
    return pins[0] if pins else None


def runs_of(
    entries: Sequence[PinEntry | RunEntry],
    dataset: str,
    version: str,
    freeze: str,
    mode: LedgerMode,
) -> list[RunEntry]:
    """The ledgered test runs of one mode on one frozen version, in order (the first is the one
    the note reports)."""
    return [
        e
        for e in entries
        if isinstance(e, RunEntry)
        and (e.dataset, e.version, e.freeze_sha256, e.mode) == (dataset, version, freeze, mode)
    ]

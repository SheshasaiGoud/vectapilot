"""M7 PR 2: the run folders' names and run.json (`redteam/runinfo.py`) and the ledger
(`redteam/ledger.py`, on `evalrun/ledger.py`)."""

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.redteam.runjson import FINGERPRINT, T0, redteam_run_info
from vectapilot_api.evalrun.ledger import LedgerEntry, append_ledger, read_ledger
from vectapilot_api.redteam.ledger import LEDGER_PATH, runs_of
from vectapilot_api.redteam.runinfo import DRY_LINE, REPLAY_LINE, Fingerprint, folder_name


def test_folder_names_carry_the_utc_start() -> None:
    later = T0.astimezone().replace(microsecond=5)
    assert folder_name(live=True, started=later) == "20261116T120000Z-live-redteam"
    assert folder_name(live=False, started=T0 + timedelta(seconds=1)).endswith("01Z-fake-redteam")


def test_the_dry_and_replay_lines_are_the_plans() -> None:
    assert DRY_LINE == (
        "DRY: deterministic guards with scripted model text; says nothing about the model's own"
        " resistance. The interval is arithmetic for scale on a constructed set, not a coverage"
        " estimate."
    )
    assert REPLAY_LINE == "agent loop: scripted replay standing in for M5 H1 `run_turn`"


def test_the_fingerprint_hash_moves_with_any_pin() -> None:
    assert FINGERPRINT.sha256() == FINGERPRINT.model_copy().sha256()
    for change in (
        {"rules_version": "b" * 64},
        {"pii_seed": 1},
        {"guard_answer": "off"},
        {"agent_loop": "replay"},
        {"gates_sha256": "b" * 64},
    ):
        moved = Fingerprint.model_validate(FINGERPRINT.model_dump() | change)
        assert moved.sha256() != FINGERPRINT.sha256(), change


@pytest.mark.parametrize(
    "change",
    [
        {"test_rerun_reason": "Because I said so"},
        {"stop": "a free-text reason"},
        {"commit": "not-a-sha"},
    ],
)
def test_run_json_takes_slugs_and_codes_never_free_text(change: dict[str, object]) -> None:
    info = redteam_run_info("t1")
    with pytest.raises(ValidationError):
        type(info).model_validate(info.model_dump() | change)


def test_the_ledger_lives_beside_the_live_folders(tmp_path: Path) -> None:
    assert LEDGER_PATH.as_posix() == "evals/reports/redteam/test-runs.jsonl"
    path = tmp_path / LEDGER_PATH
    entry = LedgerEntry(
        dataset="redteam",
        version="v0.1",
        freeze_sha256="f" * 64,
        config_fingerprint=FINGERPRINT.sha256(),
        run_id="20261116T120000Z-live-redteam",
        commit="c" * 40,
        started_at=T0,
        rerun_reason=None,
    )
    other = entry.model_copy(update={"config_fingerprint": "0" * 64, "run_id": "x"})
    for item in (entry, other, entry.model_copy(update={"rerun_reason": "provider-outage"})):
        append_ledger(path, item)
    entries = read_ledger(path)
    first, again = runs_of(entries, "redteam", "v0.1", "f" * 64, FINGERPRINT.sha256())
    assert first == entry and again.rerun_reason == "provider-outage"

"""M5's test-run ledger key (`tooleval/ledger.py`): once per config means once per dataset,
version, freeze and fingerprint, each one on its own. Unit; every entry is TEST-ONLY."""

from typing import Final

import pytest

from tests.intenteval.harness_helpers import Now
from vectapilot_api.evalrun.ledger import LedgerEntry
from vectapilot_api.tooleval.ledger import runs_of

KEY: Final = {
    "dataset": "tool_calls",
    "version": "v0.1",
    "freeze_sha256": "f" * 64,
    "config_fingerprint": "a" * 64,
}


def entry(run_id: str, **changes: str) -> LedgerEntry:
    return LedgerEntry.model_validate(
        KEY
        | changes
        | {"run_id": run_id, "commit": "c" * 40, "started_at": Now()(), "rerun_reason": None}
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"dataset": "support_qa"},
        {"version": "v0.2"},
        {"freeze_sha256": "e" * 64},
        {"config_fingerprint": "b" * 64},
    ],
    ids=["dataset", "version", "freeze", "fingerprint"],
)
def test_only_an_entry_with_the_whole_key_is_a_run_of_this_config(changes: dict[str, str]) -> None:
    same, other = entry("t1-live-agent-test"), entry("t2-live-agent-test", **changes)
    found = runs_of(
        [other, same],
        KEY["dataset"],
        KEY["version"],
        KEY["freeze_sha256"],
        KEY["config_fingerprint"],
    )
    assert found == [same]

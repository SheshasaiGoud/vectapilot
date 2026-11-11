"""M5's spend (`tooleval/spend.py`, D8: $1.00) on M3's counting core (`evalrun/spend.py`).

HEAD is a dict and every folder is TEST-ONLY in tmp_path; every dollar figure is planted and every
sum worked out by hand. M3's and M4's own tests are unchanged; here: M5's rules, and that no
milestone counts another's folders.
"""

from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Final

import pytest

from tests.evalrun.test_spend import note_json
from tests.evalrun.test_spend import run_json as m3_run_json
from tests.intenteval.test_spend import Repo as M4Repo
from tests.intenteval.test_spend import m4_run_json
from tests.tooleval.runjson import T0, tool_run_json
from vectapilot_api.evalrun.spend import (
    LIVE_FOLDER,
    RECORDED_SPEND_FILE,
    m3_spend,
    spend_refusal,
    spend_text,
)
from vectapilot_api.intenteval.runinfo import folder_name as m4_folder_name
from vectapilot_api.intenteval.spend import M4_LIVE_FOLDER, m4_spend
from vectapilot_api.tooleval.runinfo import folder_name
from vectapilot_api.tooleval.spend import (
    M5_CEILING_USD,
    M5_LIVE_BRANCH,
    M5_LIVE_FOLDER,
    M5_RULES,
    m5_run_reason,
    m5_spend,
)

M5_DIR: Final = PurePosixPath("evals/reports/tool_calls/v0.1")
M4_DIR: Final = PurePosixPath("evals/reports/intents/v0.1")
M3_DIR: Final = PurePosixPath("evals/reports/support_qa/v0.1")


class Repo(M4Repo):
    def m5(self) -> Any:
        return m5_spend(self.root, self.head_paths, self.head_file)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


def test_no_milestone_matches_another_milestones_folders() -> None:
    m5_names = [
        folder_name(live=live, started=T0, split=split)
        for live in (True, False)
        for split in ("dev", "test")
    ]
    for name in m5_names:
        for where in (M5_DIR, PurePosixPath("evals/reports/tmp/tool_calls/v0.1")):
            path = (where / name).as_posix()
            assert LIVE_FOLDER.match(path) is None, path  # M3 never counts an M5 folder
            assert M4_LIVE_FOLDER.match(path) is None, path  # nor M4
            live = where == M5_DIR and "-live-agent-" in name
            assert (M5_LIVE_FOLDER.match(path) is not None) == live, path
    # And M5 never counts M3's or M4's.
    m4_names = [
        m4_folder_name(kind, real=real, started=T0, split=split)
        for kind in ("embed", "llm", "classifier", "hybrid")
        for real in (True, False)
        for split in ((None,) if kind == "embed" else ("dev", "test"))
    ]
    for name in m4_names:
        assert M5_LIVE_FOLDER.match((M4_DIR / name).as_posix()) is None
    for name in ("20261109T120000Z-live-dev", "20261109T120000Z-live-test"):
        assert M5_LIVE_FOLDER.match((M3_DIR / name).as_posix()) is None
        # The plan's wording, `-live-<split>` under tool_calls, would have been M3's:
        assert LIVE_FOLDER.match((M5_DIR / name).as_posix()) is not None


def test_m5_counts_its_committed_live_folders_against_d8(repo: Repo) -> None:
    repo.put(M5_DIR, "t1-live-agent-dev", tool_run_json("t1-live-agent-dev", total="0.31"))
    repo.put(M5_DIR, "t2-fake-agent-dev", tool_run_json("t2-fake-agent-dev", dry=True, total="0"))
    repo.put(M4_DIR, "t3-live-llm-dev", m4_run_json("t3-live-llm-dev", total="0.40"))
    repo.put(M3_DIR, "t4-live-dev", m3_run_json("t4-live-dev", total="0.90"))

    spend = repo.m5()

    # By hand: only t1 counts; left 1.00 - 0.31 = 0.69.
    assert [(f.folder.name, f.total_usd, f.why) for f in spend.counted] == [
        ("t1-live-agent-dev", Decimal("0.31"), "live agent dev")
    ]
    assert (spend.spent, spend.left, spend.ceiling) == (
        Decimal("0.31"),
        Decimal("0.69"),
        M5_CEILING_USD,
    )
    text = spend_text(spend, "live agent test")
    assert text.startswith(
        "M5 spend so far (D8; live folders committed in HEAD that are M5's: -live-agent-<split>, or"
        " without run.json): $0.31 in 1 folder(s) (t1-live-agent-dev $0.31) · ceiling $1 · left"
        " $0.69. This run counts toward it: yes (live agent test)."
    )
    assert M5_LIVE_BRANCH == "m5/close-out" and "on m5/close-out" in M5_RULES.branch_line
    assert spend_refusal(spend, Decimal("0.69")) is None
    refusal = spend_refusal(spend, Decimal("0.70"))
    assert refusal is not None and refusal.startswith(
        "--max-usd $0.7 is more than what is left of M5's $1 ceiling (D8)"
    )


def test_m3_and_m4_sums_do_not_move_with_m5s_folders(repo: Repo) -> None:
    repo.put(M3_DIR, "t1-live-dev", m3_run_json("t1-live-dev", total="0.61"))
    repo.put(M4_DIR, "t2-live-embed", m4_run_json("t2-live-embed", total="0.30", kind="embed"))
    m3_before = m3_spend(repo.root, repo.head_paths, repo.head_file)
    m4_before = m4_spend(repo.root, repo.head_paths, repo.head_file)
    repo.put(M5_DIR, "t3-live-agent-dev", tool_run_json("t3-live-agent-dev", total="0.20"))
    repo.put(M5_DIR, "t4-live-agent-test", None, committed=False)  # an M5 run in progress

    assert m3_spend(repo.root, repo.head_paths, repo.head_file) == m3_before
    assert m4_spend(repo.root, repo.head_paths, repo.head_file) == m4_before


def test_one_m5_folder_without_run_json_uses_the_whole_ceiling(repo: Repo) -> None:
    repo.put(M5_DIR, "t1-live-agent-dev", None)  # committed, no run.json

    spend = repo.m5()

    # By hand: counted at the $1.00 per-run ceiling, which is all of D8's $1.00.
    assert (spend.spent, spend.left) == (Decimal("1.00"), Decimal(0))
    assert spend_refusal(spend, Decimal("0.01")) is not None


def test_an_uncommitted_m5_live_run_json_refuses_the_next_live_run(repo: Repo) -> None:
    repo.put(M5_DIR, "t1-live-agent-dev", tool_run_json("t1-live-agent-dev"), committed=False)

    refusal = spend_refusal(repo.m5(), Decimal("0.10"))

    assert refusal is not None and refusal.startswith("a live folder of M5 is not committed")


def test_a_recorded_spend_in_an_m5_folder_counts_whatever_its_m3_field_says(repo: Repo) -> None:
    folder = M5_DIR / "t1-live-agent-dev"
    repo.put(M5_DIR, "t1-live-agent-dev", None)  # committed, no run.json
    note = note_json("0.40", m3=False)  # `counts_toward_m3` is M3's field, never read here
    repo.head[folder / RECORDED_SPEND_FILE] = note
    (repo.root / folder / RECORDED_SPEND_FILE).write_bytes(note)

    spend = repo.m5()

    # By hand: the recorded $0.40, not the $1.00 ceiling; left 0.60.
    assert [(f.folder.name, f.total_usd, f.basis) for f in spend.counted] == [
        ("t1-live-agent-dev", Decimal("0.40"), "recorded")
    ]
    assert (spend.spent, spend.left) == (Decimal("0.40"), Decimal("0.60"))


def test_only_a_live_run_json_is_m5_spend() -> None:
    live = m5_run_reason(tool_run_json("t1-live-agent-dev", total="0.31"))
    assert live is not None
    assert (live.total_usd, live.why) == (Decimal("0.31"), "live agent dev")
    dry = m5_run_reason(tool_run_json("t2-fake-agent-dev", dry=True, total="0"))
    assert dry is not None and dry.why is None
    assert m5_run_reason(b"{}") is None

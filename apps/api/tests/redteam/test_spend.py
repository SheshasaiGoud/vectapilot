"""M7's spend (`redteam/spend.py`, D7 (b): $1.00) on M3's counting core (`evalrun/spend.py`).

HEAD is a dict and every folder is TEST-ONLY in tmp_path; every dollar figure is planted and every
sum worked out by hand. M3's to M6's own tests are unchanged; here: M7's rules, and that no
milestone counts another's folders, both ways.
"""

from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Final

import pytest

from tests.evalrun.test_spend import note_json
from tests.evalrun.test_spend import run_json as m3_run_json
from tests.extracteval.runjson import extract_run_json
from tests.intenteval.test_spend import Repo as M4Repo
from tests.intenteval.test_spend import m4_run_json
from tests.redteam.runjson import T0, redteam_run_json
from tests.tooleval.runjson import tool_run_json
from vectapilot_api.evalrun.spend import (
    LIVE_FOLDER,
    RECORDED_SPEND_FILE,
    m3_spend,
    spend_refusal,
    spend_text,
)
from vectapilot_api.extracteval.runinfo import folder_name as m6_folder_name
from vectapilot_api.extracteval.spend import M6_LIVE_FOLDER, m6_spend
from vectapilot_api.intenteval.runinfo import folder_name as m4_folder_name
from vectapilot_api.intenteval.spend import M4_LIVE_FOLDER, m4_spend
from vectapilot_api.redteam.runinfo import folder_name, run_dir
from vectapilot_api.redteam.spend import (
    M7_CEILING_USD,
    M7_LIVE_BRANCH,
    M7_LIVE_FOLDER,
    M7_RULES,
    m7_run_reason,
    m7_spend,
)
from vectapilot_api.tooleval.runinfo import folder_name as m5_folder_name
from vectapilot_api.tooleval.spend import M5_LIVE_FOLDER, m5_spend

M7_DIR: Final = PurePosixPath("evals/reports/redteam/v0.1")
M7_DRY_DIR: Final = PurePosixPath("evals/reports/tmp/redteam")
M6_DIR: Final = PurePosixPath("evals/reports/doc_extraction/v0.1")
M5_DIR: Final = PurePosixPath("evals/reports/tool_calls/v0.1")
M4_DIR: Final = PurePosixPath("evals/reports/intents/v0.1")
M3_DIR: Final = PurePosixPath("evals/reports/support_qa/v0.1")


class Repo(M4Repo):
    def m7(self) -> Any:
        return m7_spend(self.root, self.head_paths, self.head_file)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


def test_m7s_folders_match_no_other_milestones_pattern() -> None:
    for live in (True, False):
        name = folder_name(live=live, started=T0)
        assert name == (
            "20261116T120000Z-live-redteam" if live else "20261116T120000Z-fake-redteam"
        )
        for where in (M7_DIR, M7_DRY_DIR):
            path = (where / name).as_posix()
            for other in (LIVE_FOLDER, M4_LIVE_FOLDER, M5_LIVE_FOLDER, M6_LIVE_FOLDER):
                assert other.match(path) is None, (other.pattern, path)
            counts = where == M7_DIR and live
            assert (M7_LIVE_FOLDER.match(path) is not None) == counts, path
    root = Path("/r")
    assert run_dir(root, "v0.1", "n", live=True) == root / M7_DIR / "n"
    assert run_dir(root, "v0.1", "n", live=False) == root / M7_DRY_DIR / "n"  # no version level


def test_no_other_milestones_folder_matches_m7s_pattern() -> None:
    m6_names = [
        m6_folder_name(live=live, started=T0, split=s)
        for live in (True, False)
        for s in ("dev", "test")
    ]
    m5_names = [
        m5_folder_name(live=live, started=T0, split=s)
        for live in (True, False)
        for s in ("dev", "test")
    ]
    m4_names = [
        m4_folder_name(kind, real=real, started=T0, split=split)
        for kind in ("embed", "llm", "classifier", "hybrid")
        for real in (True, False)
        for split in ((None,) if kind == "embed" else ("dev", "test"))
    ]
    m3_names = ["20261109T120000Z-live-dev", "20261109T120000Z-live-test"]
    for where, names in (
        (M6_DIR, m6_names),
        (M5_DIR, m5_names),
        (M4_DIR, m4_names),
        (M3_DIR, m3_names),
    ):
        for name in names:
            assert M7_LIVE_FOLDER.match((where / name).as_posix()) is None, name


def test_m7s_live_folder_rules_are_the_plans() -> None:
    assert M7_LIVE_FOLDER.pattern == r"^evals/reports/redteam/[^/]+/[^/]+-live-redteam$"
    assert M7_RULES.disk_glob == "redteam/*/*-live-*"
    assert (Decimal("1.00"), "m7/close-out") == (M7_CEILING_USD, M7_LIVE_BRANCH)
    assert "on m7/close-out" in M7_RULES.branch_line
    for path in (
        "evals/reports/support_qa/v0.1/t1-live-redteam",  # another dataset's folder
        "evals/reports/redteam/v0.1/t1-live-redteamx",
        "evals/reports/redteam/t1-live-redteam",  # no version level
        "evals/reports/redteam/v0.1/t1-live-redteam/run.json",
    ):
        assert M7_LIVE_FOLDER.match(path) is None, path


def test_m7_counts_its_committed_live_folders_against_d7(repo: Repo) -> None:
    repo.put(M7_DIR, "t1-live-redteam", redteam_run_json("t1-live-redteam", total="0.27"))
    repo.put(M6_DIR, "t2-live-extract-dev", extract_run_json("t2-live-extract-dev", total="0.30"))
    repo.put(M5_DIR, "t3-live-agent-dev", tool_run_json("t3-live-agent-dev", total="0.30"))
    repo.put(M4_DIR, "t4-live-llm-dev", m4_run_json("t4-live-llm-dev", total="0.40"))
    repo.put(M3_DIR, "t5-live-dev", m3_run_json("t5-live-dev", total="0.90"))

    spend = repo.m7()

    # By hand: only t1 counts; left 1.00 - 0.27 = 0.73.
    assert [(f.folder.name, f.total_usd, f.why) for f in spend.counted] == [
        ("t1-live-redteam", Decimal("0.27"), "live redteam")
    ]
    assert (spend.spent, spend.left, spend.ceiling) == (
        Decimal("0.27"),
        Decimal("0.73"),
        M7_CEILING_USD,
    )
    assert spend_text(spend, "live redteam").startswith(
        "M7 spend so far (D7; live folders committed in HEAD that are M7's: -live-redteam, or"
        " without run.json): $0.27 in 1 folder(s) (t1-live-redteam $0.27) · ceiling $1 ·"
        " left $0.73. This run counts toward it: yes (live redteam)."
    )
    assert spend_refusal(spend, Decimal("0.73")) is None
    refusal = spend_refusal(spend, Decimal("0.74"))
    assert refusal is not None and refusal.startswith(
        "--max-usd $0.74 is more than what is left of M7's $1 ceiling (D7)"
    )


def test_m3_to_m6_sums_do_not_move_with_m7s_folders(repo: Repo) -> None:
    repo.put(M3_DIR, "t1-live-dev", m3_run_json("t1-live-dev", total="0.61"))
    repo.put(M4_DIR, "t2-live-embed", m4_run_json("t2-live-embed", total="0.30", kind="embed"))
    repo.put(M5_DIR, "t3-live-agent-dev", tool_run_json("t3-live-agent-dev", total="0.20"))
    repo.put(M6_DIR, "t4-live-extract-dev", extract_run_json("t4", total="0.10"))
    spenders = (m3_spend, m4_spend, m5_spend, m6_spend)
    before = [spend(repo.root, repo.head_paths, repo.head_file) for spend in spenders]
    repo.put(M7_DIR, "t5-live-redteam", redteam_run_json("t5", total="0.20"))
    repo.put(M7_DIR, "t6-live-redteam", None, committed=False)  # an M7 run in progress

    after = [spend(repo.root, repo.head_paths, repo.head_file) for spend in spenders]
    assert after == before


def test_one_m7_folder_without_run_json_uses_the_whole_ceiling(repo: Repo) -> None:
    repo.put(M7_DIR, "t1-live-redteam", None)  # committed, no run.json

    spend = repo.m7()

    # By hand: counted at the $1.00 per-run ceiling, which is all of D7's $1.00.
    assert (spend.spent, spend.left) == (Decimal("1.00"), Decimal(0))
    assert spend_refusal(spend, Decimal("0.01")) is not None


def test_an_uncommitted_m7_live_run_json_refuses_the_next_live_run(repo: Repo) -> None:
    repo.put(M7_DIR, "t1-live-redteam", redteam_run_json("t1"), committed=False)

    refusal = spend_refusal(repo.m7(), Decimal("0.10"))

    assert refusal is not None and refusal.startswith("a live folder of M7 is not committed")


def test_a_recorded_spend_in_an_m7_folder_counts_whatever_its_m3_field_says(repo: Repo) -> None:
    folder = M7_DIR / "t1-live-redteam"
    repo.put(M7_DIR, "t1-live-redteam", None)  # committed, no run.json
    note = note_json("0.35", m3=False)  # `counts_toward_m3` is M3's field, never read here
    repo.head[folder / RECORDED_SPEND_FILE] = note
    (repo.root / folder / RECORDED_SPEND_FILE).write_bytes(note)

    spend = repo.m7()

    # By hand: the recorded $0.35, not the $1.00 ceiling; left 0.65.
    assert [(f.folder.name, f.total_usd, f.basis) for f in spend.counted] == [
        ("t1-live-redteam", Decimal("0.35"), "recorded")
    ]
    assert (spend.spent, spend.left) == (Decimal("0.35"), Decimal("0.65"))


def test_only_a_live_run_json_is_m7_spend() -> None:
    live = m7_run_reason(redteam_run_json("t1", total="0.27"))
    assert live is not None and (live.total_usd, live.why) == (Decimal("0.27"), "live redteam")
    dry = m7_run_reason(redteam_run_json("t2", dry=True, total="0"))
    assert dry is not None and dry.why is None
    assert m7_run_reason(b"{}") is None
    assert m7_run_reason(tool_run_json("t3")) is None  # another milestone's run.json

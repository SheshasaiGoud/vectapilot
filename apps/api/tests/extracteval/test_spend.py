"""M6's spend (`extracteval/spend.py`, D8 (b): $1.00) on M3's counting core (`evalrun/spend.py`).

HEAD is a dict and every folder is TEST-ONLY in tmp_path; every dollar figure is planted and every
sum worked out by hand. M3's, M4's and M5's own tests are unchanged; here: M6's rules, and that no
milestone counts another's folders, both ways.
"""

from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Final

import pytest

from tests.evalrun.test_spend import note_json
from tests.evalrun.test_spend import run_json as m3_run_json
from tests.extracteval.runjson import T0, extract_run_json
from tests.intenteval.test_spend import Repo as M4Repo
from tests.intenteval.test_spend import m4_run_json
from tests.tooleval.runjson import tool_run_json
from vectapilot_api.evalrun.spend import (
    LIVE_FOLDER,
    RECORDED_SPEND_FILE,
    m3_spend,
    spend_refusal,
    spend_text,
)
from vectapilot_api.extracteval.runinfo import folder_name, run_dir
from vectapilot_api.extracteval.spend import (
    M6_CEILING_USD,
    M6_LIVE_BRANCH,
    M6_LIVE_FOLDER,
    M6_RULES,
    m6_run_reason,
    m6_spend,
)
from vectapilot_api.intenteval.runinfo import folder_name as m4_folder_name
from vectapilot_api.intenteval.spend import M4_LIVE_FOLDER, m4_spend
from vectapilot_api.tooleval.runinfo import folder_name as m5_folder_name
from vectapilot_api.tooleval.spend import M5_LIVE_FOLDER, m5_spend

M6_DIR: Final = PurePosixPath("evals/reports/doc_extraction/v0.1")
M6_DRY_DIR: Final = PurePosixPath("evals/reports/tmp/doc_extraction/v0.1")
M5_DIR: Final = PurePosixPath("evals/reports/tool_calls/v0.1")
M4_DIR: Final = PurePosixPath("evals/reports/intents/v0.1")
M3_DIR: Final = PurePosixPath("evals/reports/support_qa/v0.1")


class Repo(M4Repo):
    def m6(self) -> Any:
        return m6_spend(self.root, self.head_paths, self.head_file)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


def test_m6s_folders_match_no_other_milestones_pattern() -> None:
    for live in (True, False):
        for split in ("dev", "test"):
            name = folder_name(live=live, started=T0, split=split)
            for where in (M6_DIR, M6_DRY_DIR):
                path = (where / name).as_posix()
                assert LIVE_FOLDER.match(path) is None, path  # M3 never counts an M6 folder
                assert M4_LIVE_FOLDER.match(path) is None, path  # nor M4
                assert M5_LIVE_FOLDER.match(path) is None, path  # nor M5
                counts = where == M6_DIR and live
                assert (M6_LIVE_FOLDER.match(path) is not None) == counts, path
    # The plan's bare `-live-<split>` under doc_extraction would have been M3's:
    assert LIVE_FOLDER.match((M6_DIR / "20261113T120000Z-live-dev").as_posix()) is not None
    root = Path("/r")
    assert run_dir(root, "v0.1", "n", live=True) == root / M6_DIR / "n"
    assert run_dir(root, "v0.1", "n", live=False) == root / M6_DRY_DIR / "n"


def test_no_other_milestones_folder_matches_m6s_pattern() -> None:
    m5_names = [
        m5_folder_name(live=live, started=T0, split=split)
        for live in (True, False)
        for split in ("dev", "test")
    ]
    m4_names = [
        m4_folder_name(kind, real=real, started=T0, split=split)
        for kind in ("embed", "llm", "classifier", "hybrid")
        for real in (True, False)
        for split in ((None,) if kind == "embed" else ("dev", "test"))
    ]
    m3_names = ["20261109T120000Z-live-dev", "20261109T120000Z-live-test"]
    for where, names in ((M5_DIR, m5_names), (M4_DIR, m4_names), (M3_DIR, m3_names)):
        for name in names:
            assert M6_LIVE_FOLDER.match((where / name).as_posix()) is None, name


def test_m6_counts_its_committed_live_folders_against_d8(repo: Repo) -> None:
    repo.put(M6_DIR, "t1-live-extract-dev", extract_run_json("t1-live-extract-dev", total="0.27"))
    repo.put(M6_DIR, "t2-fake-extract-dev", extract_run_json("t2", dry=True, total="0"))
    repo.put(M5_DIR, "t3-live-agent-dev", tool_run_json("t3-live-agent-dev", total="0.30"))
    repo.put(M4_DIR, "t4-live-llm-dev", m4_run_json("t4-live-llm-dev", total="0.40"))
    repo.put(M3_DIR, "t5-live-dev", m3_run_json("t5-live-dev", total="0.90"))

    spend = repo.m6()

    # By hand: only t1 counts; left 1.00 - 0.27 = 0.73.
    assert [(f.folder.name, f.total_usd, f.why) for f in spend.counted] == [
        ("t1-live-extract-dev", Decimal("0.27"), "live extract dev")
    ]
    assert (spend.spent, spend.left, spend.ceiling) == (
        Decimal("0.27"),
        Decimal("0.73"),
        M6_CEILING_USD,
    )
    text = spend_text(spend, "live extract test")
    assert text.startswith(
        "M6 spend so far (D8; live folders committed in HEAD that are M6's: -live-extract-<split>,"
        " or without run.json): $0.27 in 1 folder(s) (t1-live-extract-dev $0.27) · ceiling $1 ·"
        " left $0.73. This run counts toward it: yes (live extract test)."
    )
    assert M6_LIVE_BRANCH == "m6/close-out" and "on m6/close-out" in M6_RULES.branch_line
    assert spend_refusal(spend, Decimal("0.73")) is None
    refusal = spend_refusal(spend, Decimal("0.74"))
    assert refusal is not None and refusal.startswith(
        "--max-usd $0.74 is more than what is left of M6's $1 ceiling (D8)"
    )


def test_m3_m4_and_m5_sums_do_not_move_with_m6s_folders(repo: Repo) -> None:
    repo.put(M3_DIR, "t1-live-dev", m3_run_json("t1-live-dev", total="0.61"))
    repo.put(M4_DIR, "t2-live-embed", m4_run_json("t2-live-embed", total="0.30", kind="embed"))
    repo.put(M5_DIR, "t3-live-agent-dev", tool_run_json("t3-live-agent-dev", total="0.20"))
    before = [
        spend(repo.root, repo.head_paths, repo.head_file)
        for spend in (m3_spend, m4_spend, m5_spend)
    ]
    repo.put(M6_DIR, "t4-live-extract-dev", extract_run_json("t4", total="0.20"))
    repo.put(M6_DIR, "t5-live-extract-test", None, committed=False)  # an M6 run in progress

    after = [
        spend(repo.root, repo.head_paths, repo.head_file)
        for spend in (m3_spend, m4_spend, m5_spend)
    ]
    assert after == before


def test_one_m6_folder_without_run_json_uses_the_whole_ceiling(repo: Repo) -> None:
    repo.put(M6_DIR, "t1-live-extract-dev", None)  # committed, no run.json

    spend = repo.m6()

    # By hand: counted at the $1.00 per-run ceiling, which is all of D8's $1.00.
    assert (spend.spent, spend.left) == (Decimal("1.00"), Decimal(0))
    assert spend_refusal(spend, Decimal("0.01")) is not None


@pytest.mark.parametrize("split", ["dev", "test"])
def test_an_uncommitted_m6_live_run_json_refuses_the_next_live_run(repo: Repo, split: str) -> None:
    # Either split: an uncommitted live test folder is spend too (mutation audit SP05).
    name = f"t1-live-extract-{split}"
    repo.put(M6_DIR, name, extract_run_json(name, split=split), committed=False)

    refusal = spend_refusal(repo.m6(), Decimal("0.10"))

    assert refusal is not None and refusal.startswith("a live folder of M6 is not committed")


def test_m6s_live_folder_rules_are_the_plans() -> None:
    # The plan pins the pattern literally; the disk glob finds every live folder, test included
    # (mutation audit SP05, SP07, SP08).
    assert M6_LIVE_FOLDER.pattern == (
        r"^evals/reports/doc_extraction/[^/]+/[^/]+-live-extract-(?:dev|test)$"
    )
    assert M6_RULES.disk_glob == "doc_extraction/*/*-live-*"
    for path in (
        "evals/reports/support_qa/v0.1/t1-live-extract-dev",  # another dataset's folder
        "evals/reports/doc_extraction/v0.1/t1-live-extract-devx",
        "evals/reports/doc_extraction/v0.1/t1-live-extract-test/run.json",
    ):
        assert M6_LIVE_FOLDER.match(path) is None, path


def test_a_recorded_spend_in_an_m6_folder_counts_whatever_its_m3_field_says(repo: Repo) -> None:
    folder = M6_DIR / "t1-live-extract-dev"
    repo.put(M6_DIR, "t1-live-extract-dev", None)  # committed, no run.json
    note = note_json("0.35", m3=False)  # `counts_toward_m3` is M3's field, never read here
    repo.head[folder / RECORDED_SPEND_FILE] = note
    (repo.root / folder / RECORDED_SPEND_FILE).write_bytes(note)

    spend = repo.m6()

    # By hand: the recorded $0.35, not the $1.00 ceiling; left 0.65.
    assert [(f.folder.name, f.total_usd, f.basis) for f in spend.counted] == [
        ("t1-live-extract-dev", Decimal("0.35"), "recorded")
    ]
    assert (spend.spent, spend.left) == (Decimal("0.35"), Decimal("0.65"))


def test_only_a_live_run_json_is_m6_spend() -> None:
    live = m6_run_reason(extract_run_json("t1", total="0.27", split="test"))
    assert live is not None
    assert (live.total_usd, live.why) == (Decimal("0.27"), "live extract test")
    dry = m6_run_reason(extract_run_json("t2", dry=True, total="0"))
    assert dry is not None and dry.why is None
    assert m6_run_reason(b"{}") is None
    assert m6_run_reason(tool_run_json("t3")) is None  # another milestone's run.json

"""M4's spend (`intenteval/spend.py`, D7: $2.00) on M3's counting core (`evalrun/spend.py`).

HEAD is a dict and every folder is TEST-ONLY in tmp_path; every dollar figure is planted and every
sum worked out by hand. M3's own tests (tests/evalrun/test_spend.py) are unchanged; here: M4's
rules, and that the two milestones never count each other's folders.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Final

import pytest

from tests.evalrun.test_spend import Repo as M3Repo
from tests.evalrun.test_spend import run_json as m3_run_json
from vectapilot_api.evalrun.report import CostInfo
from vectapilot_api.evalrun.spend import (
    LIVE_FOLDER,
    RECORDED_SPEND_FILE,
    m3_spend,
    spend_refusal,
    spend_text,
)
from vectapilot_api.intenteval.runinfo import Fingerprint, IntentRunInfo, folder_name
from vectapilot_api.intenteval.spend import (
    M4_CEILING_USD,
    M4_LIVE_BRANCH,
    M4_LIVE_FOLDER,
    M4_RULES,
    m4_run_reason,
    m4_spend,
)

M4_DIR: Final = PurePosixPath("evals/reports/intents/v0.1")
M3_DIR: Final = PurePosixPath("evals/reports/support_qa/v0.1")
T0: Final = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def m4_run_json(
    name: str,
    *,
    total: str = "0.40",
    kind: str = "llm",
    dry: bool = False,
    llm_cost_complete: bool = True,
) -> bytes:
    fingerprint = Fingerprint(freeze_sha256="f" * 64)
    info = IntentRunInfo(
        kind=kind,
        run_id=name,
        status="complete",
        dry_run=dry,
        environment="local",
        dataset="intents",
        dataset_version="v0.1",
        dataset_stage="frozen",
        freeze_sha256="f" * 64,
        dataset_committed=True,
        split=None if kind == "embed" else "dev",
        rows=10,
        commit="c" * 40,
        uncommitted_changes=False,
        started_at=T0,
        ended_at=T0,
        fingerprint=fingerprint,
        fingerprint_sha256=fingerprint.sha256(),
        confidence="19/20",
        bootstrap_seed=1,
        bootstrap_resamples=10,
        cost=CostInfo(
            cap_usd=Decimal("1.00"),
            embedding_usd=Decimal(total) if kind == "embed" else Decimal(0),
            llm_usd=Decimal(0) if kind == "embed" else Decimal(total),
            total_usd=Decimal(total),
            llm_cost_complete=llm_cost_complete,
            embedding_requests=1,
            llm_calls=1,
        ),
    )
    return (info.model_dump_json(indent=2) + "\n").encode("utf-8")


class Repo(M3Repo):
    """M3's TEST-ONLY repo, with folders under any version directory."""

    def put(
        self,
        where: PurePosixPath,
        name: str,
        content: bytes | None,
        *,
        committed: bool = True,
        extra: str = "results.jsonl",
    ) -> None:
        folder = where / name
        path = self.root / folder
        path.mkdir(parents=True, exist_ok=True)
        (path / extra).write_bytes(b"")
        if committed:
            self.head[folder / extra] = b""
        if content is not None:
            (path / "run.json").write_bytes(content)
            if committed:
                self.head[folder / "run.json"] = content

    def m4(self) -> Any:
        return m4_spend(self.root, self.head_paths, self.head_file)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


def test_every_m4_folder_name_misses_m3s_pattern_and_only_live_ones_are_m4s() -> None:
    names = [
        folder_name(kind, real=real, started=T0, split=split)
        for kind in ("embed", "model", "classifier", "llm", "hybrid")
        for real in (True, False)
        for split in ((None,) if kind in ("embed", "model") else ("dev", "test"))
    ]
    paid = {n for n in names if n.endswith(("-live-embed", "-live-llm-dev", "-live-llm-test"))}

    assert len(names) == 16 and len(paid) == 3
    for name in names:
        for where in (M4_DIR, PurePosixPath("evals/reports/tmp/intents/v0.1")):
            path = (where / name).as_posix()
            assert LIVE_FOLDER.match(path) is None, path  # M3 never counts an M4 folder
            assert (M4_LIVE_FOLDER.match(path) is not None) == (where == M4_DIR and name in paid), (
                path
            )
    # And M4 never counts M3's.
    assert M4_LIVE_FOLDER.match((M3_DIR / "20261109T120000Z-live-dev").as_posix()) is None


def test_m4_counts_its_committed_live_folders_against_d7(repo: Repo) -> None:
    repo.put(M4_DIR, "t1-live-embed", m4_run_json("t1-live-embed", total="0.12", kind="embed"))
    repo.put(M4_DIR, "t2-live-llm-dev", m4_run_json("t2-live-llm-dev", total="0.40"))
    # $0 folders are never counted, committed or not.
    repo.put(M4_DIR, "t3-local-classifier-dev", m4_run_json("t3", kind="classifier"))
    repo.put(M4_DIR, "t4-replay-hybrid-dev", m4_run_json("t4", kind="hybrid"))
    # An M3 hybrid folder: M3's, never M4's.
    repo.put(M3_DIR, "t5-live-dev", m3_run_json("t5-live-dev", total="0.90"))

    spend = repo.m4()

    # By hand: 0.12 + 0.40 = 0.52; left 2.00 - 0.52 = 1.48.
    assert [(f.folder.name, f.total_usd, f.why) for f in spend.counted] == [
        ("t1-live-embed", Decimal("0.12"), "live embed"),
        ("t2-live-llm-dev", Decimal("0.40"), "live llm dev"),
    ]
    assert (spend.spent, spend.left, spend.ceiling) == (
        Decimal("0.52"),
        Decimal("1.48"),
        M4_CEILING_USD,
    )
    text = spend_text(spend, "live llm test")
    assert text.startswith(
        "M4 spend so far (D7; live folders committed in HEAD that are M4's: -live-embed and"
        " -live-llm-<split>, or without run.json): $0.52 in 2 folder(s) (t1-live-embed $0.12,"
        " t2-live-llm-dev $0.4) · ceiling $2 · left $1.48. This run counts toward it: yes (live"
        " llm test)."
    )
    assert text.splitlines()[-1] == M4_RULES.branch_line
    assert M4_LIVE_BRANCH == "m4/close-out" and "on m4/close-out" in M4_RULES.branch_line


def test_m3s_sum_does_not_move_with_m4s_folders(repo: Repo) -> None:
    repo.put(M3_DIR, "t1-live-dev", m3_run_json("t1-live-dev", total="0.61"))
    before = m3_spend(repo.root, repo.head_paths, repo.head_file)
    repo.put(M4_DIR, "t2-live-embed", m4_run_json("t2-live-embed", total="0.30", kind="embed"))
    repo.put(M4_DIR, "t3-live-llm-test", None, committed=False)  # an M4 run in progress
    after = m3_spend(repo.root, repo.head_paths, repo.head_file)

    assert before == after
    assert (after.spent, after.uncommitted) == (Decimal("0.61"), ())


def test_an_m4_live_folder_without_run_json_counts_at_the_ceiling_or_as_recorded(
    repo: Repo,
) -> None:
    repo.put(M4_DIR, "t1-live-llm-dev", None)  # committed, no run.json
    repo.put(M4_DIR, "t2-live-embed", None, committed=False)  # on disk only

    spend = repo.m4()
    # By hand: 1.00 + 1.00 = 2.00 spent, nothing left: any cap is refused.
    assert (spend.spent, spend.left) == (Decimal("2.00"), Decimal(0))
    refusal = spend_refusal(spend, Decimal("0.01"))
    assert refusal is not None
    assert refusal.startswith("--max-usd $0.01 is more than what is left of M4's $2 ceiling (D7)")

    # A recorded spend counts whatever its M3 field says: every folder M4's pattern matches is
    # M4's by name.
    note = {
        "total_usd": "0.25",
        "counts_toward_m3": False,
        "recorded_by": "shesha",
        "recorded_on": "2026-11-09",
        "source": "TEST-ONLY",
    }
    content = json.dumps(note).encode()
    (repo.root / M4_DIR / "t1-live-llm-dev" / RECORDED_SPEND_FILE).write_bytes(content)
    repo.head[M4_DIR / "t1-live-llm-dev" / RECORDED_SPEND_FILE] = content
    assert [(f.folder.name, f.total_usd, f.basis) for f in repo.m4().counted] == [
        ("t1-live-llm-dev", Decimal("0.25"), "recorded"),
        ("t2-live-embed", Decimal("1.00"), "ceiling"),
    ]


def test_an_uncommitted_m4_live_run_json_refuses_the_next_live_run(repo: Repo) -> None:
    repo.put(M4_DIR, "t1-live-llm-dev", m4_run_json("t1-live-llm-dev"), committed=False)

    spend = repo.m4()
    refusal = spend_refusal(spend, Decimal("0.10"))

    assert spend.uncommitted == (M4_DIR / "t1-live-llm-dev",)
    assert refusal is not None and refusal.startswith("a live folder of M4 is not committed")
    assert "M4's spend counts committed folders only (D7)" in refusal


def test_the_reason_reads_m4s_run_json_only() -> None:
    spend = m4_run_reason(m4_run_json("x", total="0.33"))
    assert spend is not None and (spend.total_usd, spend.why) == (Decimal("0.33"), "live llm dev")
    dry = m4_run_reason(m4_run_json("x", dry=True))
    assert dry is not None and dry.why is None
    assert m4_run_reason(b"{}") is None
    assert m4_run_reason(m3_run_json("x")) is None  # M3's run.json is not M4's


def test_an_m4_run_with_an_incomplete_llm_cost_is_a_lower_bound(repo: Repo) -> None:
    name = "t1-live-llm-dev"
    repo.put(M4_DIR, name, m4_run_json(name, total="0.30", llm_cost_complete=False))
    repo.put(M4_DIR, "t2-live-embed", m4_run_json("t2-live-embed", total="0.10", kind="embed"))

    spend = repo.m4()

    assert spend.lower_bound and spend.spent == Decimal("0.40")
    assert [f.folder.name for f in spend.counted if not f.cost_complete] == [name]
    text = spend_text(spend, "live llm test")
    assert "$0.4 (lower bound" in text and "left $1.6 at most" in text

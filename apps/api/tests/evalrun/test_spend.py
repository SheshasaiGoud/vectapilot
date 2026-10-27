"""M3's spend across its live runs (`evalrun/spend.py`, M3 PR 5, D15 (iii)).

HEAD is a dict here ({path: bytes}, as `tests/evalrun/test_cli.py`'s fake git), and every run
folder is TEST-ONLY in pytest's tmp_path: nothing under evals/reports is read or written. Every
dollar figure is planted and every sum is worked out by hand. Two tests read the real repository's
HEAD with `git ls-tree` and `git rev-parse` (read-only).
"""

import json
import re
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Final

import pytest

from tests.evalrun.helpers import CONFIG, HYBRID_CONFIG, REPO, run_info
from vectapilot_api.evalrun.report import RUN_FILE, CostInfo, m3_spend_reason
from vectapilot_api.evalrun.spend import (
    BRANCH_LINE,
    M3_CEILING_USD,
    M3_LIVE_BRANCH,
    RECORDED_SPEND_FILE,
    M3Spend,
    RecordedSpend,
    SpendError,
    m3_spend,
    read_head_paths,
    read_head_ref,
    spend_refusal,
    spend_text,
)
from vectapilot_api.llm_cli import GitError

VERSION_DIR: Final = PurePosixPath("evals/reports/support_qa/v0.1")
DAY: Final = "2026-11-02"


def cost(total: str, *, complete: bool = True) -> CostInfo:
    return CostInfo(
        cap_usd=Decimal("1.00"),
        embedding_usd=Decimal(0),
        llm_usd=Decimal(total),
        total_usd=Decimal(total),
        llm_cost_complete=complete,
        embedding_requests=1,
        llm_calls=1,
    )


def run_json(name: str, *, total: str = "0.50", **changes: Any) -> bytes:
    values: dict[str, Any] = {
        "run_id": name,
        "dry_run": False,
        "config": HYBRID_CONFIG,
        "cost": cost(total),
    }
    return (run_info(**(values | changes)).model_dump_json(indent=2) + "\n").encode("utf-8")


class Repo:
    """A TEST-ONLY working tree in tmp_path and its HEAD as a dict."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.head: dict[PurePosixPath, bytes] = {}

    def folder(
        self,
        name: str,
        content: bytes | None,
        *,
        committed: bool = True,
        on_disk: bool = True,
        extra: str | None = "results.jsonl",
    ) -> PurePosixPath:
        folder = VERSION_DIR / name
        if committed:
            if content is not None:
                self.head[folder / RUN_FILE] = content
            if extra is not None:
                self.head[folder / extra] = b""
        if on_disk:
            path = self.root / folder
            path.mkdir(parents=True, exist_ok=True)
            if content is not None:
                (path / RUN_FILE).write_bytes(content)
            if extra is not None:
                (path / extra).write_bytes(b"")
        return folder

    def note(
        self, name: str, content: bytes, *, committed: bool = True, on_disk: bool = True
    ) -> None:
        """A recorded-spend.json in the folder, in HEAD and/or on disk."""
        folder = VERSION_DIR / name
        if committed:
            self.head[folder / RECORDED_SPEND_FILE] = content
        if on_disk:
            path = self.root / folder
            path.mkdir(parents=True, exist_ok=True)
            (path / RECORDED_SPEND_FILE).write_bytes(content)

    def head_paths(self, root: Path, under: PurePosixPath) -> list[PurePosixPath]:
        assert root == self.root
        return [path for path in self.head if path.is_relative_to(under)]

    def head_file(self, root: Path, path: PurePosixPath) -> bytes | None:
        assert root == self.root
        return self.head.get(path)

    def spend(self) -> M3Spend:
        return m3_spend(self.root, self.head_paths, self.head_file)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


def test_the_sum_counts_committed_hybrid_and_named_vector_folders_only(repo: Repo) -> None:
    repo.folder("t1-live-dev", run_json("t1-live-dev", total="0.61"))
    repo.folder("t2-live-dev", run_json("t2-live-dev", total="0.25", config=CONFIG, m3_spend=True))
    # Not M3's: a vector run without --m3-spend (M2's), and a dry run (never in HEAD in
    # practice; a dry folder costs nothing anyway).
    repo.folder("t3-live-dev", run_json("t3-live-dev", total="0.90", config=CONFIG))
    repo.folder("t4-live-test", run_json("t4-live-test", total="0.40", dry_run=True))

    spend = repo.spend()

    # 0.61 + 0.25 = 0.86; left 5.00 - 0.86 = 4.14.
    assert [(f.folder.name, f.total_usd, f.why) for f in spend.counted] == [
        ("t1-live-dev", Decimal("0.61"), "hybrid (retrieval block)"),
        ("t2-live-dev", Decimal("0.25"), "vector, named by --m3-spend"),
    ]
    assert (spend.spent, spend.left, spend.lower_bound) == (
        Decimal("0.86"),
        Decimal("4.14"),
        False,
    )
    assert spend.ceiling == M3_CEILING_USD == Decimal("5.00")
    assert spend.uncommitted == () and spend.at_ceiling == ()
    assert spend_text(spend, "hybrid (retrieval block)") == (
        "M3 spend so far (D15 (iii); live folders committed in HEAD that are M3's: hybrid, named"
        " by --m3-spend, or without run.json): $0.86 in 2 folder(s) (t1-live-dev $0.61,"
        " t2-live-dev $0.25) · ceiling $5 · left $4.14. This run counts toward it: yes (hybrid"
        " (retrieval block)). Like the cap, this only catches mistakes: the providers' own limits"
        " are the real cap (ADR 0007).\n"
        "Make every M3 live run, and commit its folder, on m3/close-out (decided 2026-10-27):"
        " this sum reads the current branch's HEAD only, so a folder committed on another branch"
        " is not counted."
    )


def test_an_incomplete_answer_model_cost_makes_the_sum_a_lower_bound(repo: Repo) -> None:
    repo.folder("t1-live-dev", run_json("t1-live-dev", total="0.30"))
    incomplete = run_info(
        run_id="t2-live-dev", dry_run=False, config=HYBRID_CONFIG, cost=cost("0.20", complete=False)
    )
    repo.folder("t2-live-dev", incomplete.model_dump_json().encode())

    spend = repo.spend()

    assert (spend.spent, spend.left, spend.lower_bound) == (
        Decimal("0.50"),
        Decimal("4.50"),
        True,
    )
    text = spend_text(spend, None)
    assert (
        "$0.5 (lower bound: a counted folder's answer-model cost was incomplete) in 2 folder(s)"
    ) in text
    assert "left $4.5 at most. This run counts toward it: no." in text


def test_only_head_counts_never_the_working_tree(repo: Repo) -> None:
    committed = run_json("t1-live-dev", total="0.30")
    repo.folder("t1-live-dev", committed)
    # On disk the same folder now says $0.99 (edited): the sum stays HEAD's, and the folder is
    # reported as not committed as on disk.
    (repo.root / VERSION_DIR / "t1-live-dev" / RUN_FILE).write_bytes(
        run_json("t1-live-dev", total="0.99")
    )

    spend = repo.spend()

    assert spend.spent == Decimal("0.30")
    assert spend.uncommitted == (VERSION_DIR / "t1-live-dev",)


def test_a_counted_folder_not_committed_yet_is_seen_and_refused(repo: Repo) -> None:
    repo.folder("t1-live-dev", run_json("t1-live-dev", total="0.30"))
    repo.folder("t2-live-dev", run_json("t2-live-dev", total="0.70"), committed=False)
    # A vector folder of M2's, not committed: not M3's business.
    repo.folder("t3-live-dev", run_json("t3-live-dev", config=CONFIG), committed=False)

    spend = repo.spend()

    assert spend.spent == Decimal("0.30")
    assert spend.uncommitted == (VERSION_DIR / "t2-live-dev",)
    assert spend_refusal(spend, Decimal("0.10")) == (
        f"a live folder of M3 is not committed as it is on disk ({VERSION_DIR}/t2-live-dev): M3's"
        " spend counts committed folders only (D15 (iii)), so commit each live folder before the"
        " next live run"
    )
    assert (
        "Not committed as on disk (a live run of M3 is refused until it is):"
        f" {VERSION_DIR}/t2-live-dev"
    ) in spend_text(spend, None).splitlines()


def test_an_unreadable_run_json_on_disk_is_treated_as_m3s(repo: Repo) -> None:
    repo.folder("t1-live-dev", b"{not json", committed=False)

    assert repo.spend().uncommitted == (VERSION_DIR / "t1-live-dev",)


def note_json(total: object = "0.42", *, m3: bool = True, **changes: Any) -> bytes:
    """A TEST-ONLY recorded-spend.json (decided 2026-10-27, shesha; runbook §6)."""
    values: dict[str, Any] = {
        "total_usd": total,
        "counts_toward_m3": m3,
        "recorded_by": "shesha",
        "recorded_on": "2026-11-02",
        "source": "TEST-ONLY: the printed counted spend",
    }
    return json.dumps(values | changes).encode("utf-8")


def test_a_live_folder_without_run_json_counts_at_the_per_run_ceiling(repo: Repo) -> None:
    # Decided 2026-10-27 (shesha): a live folder without run.json (a kill, a failed write, or a
    # run still going in another terminal) counts at M1's $1.00 per-run ceiling until its real
    # spend is recorded. By hand: 1.00 + 1.00 = 2.00 spent, 3.00 left.
    repo.folder("t1-live-dev", None, extra="results.jsonl")  # committed, no run.json
    repo.folder("t2-live-test", None, committed=False, extra="llm_call_log.jsonl")  # disk only
    repo.folder("t3-fake-dev", None, committed=False)  # not a live folder's name

    spend = repo.spend()

    assert [(f.folder.name, f.total_usd, f.basis) for f in spend.counted] == [
        ("t1-live-dev", Decimal("1.00"), "ceiling"),
        ("t2-live-test", Decimal("1.00"), "ceiling"),
    ]
    assert spend.at_ceiling == (VERSION_DIR / "t1-live-dev", VERSION_DIR / "t2-live-test")
    assert (spend.spent, spend.left, spend.lower_bound) == (
        Decimal("2.00"),
        Decimal("3.00"),
        False,
    )
    assert spend.uncommitted == ()  # a run still going is counted, never refused for it
    lines = spend_text(spend, None).splitlines()
    assert (
        "$2 in 2 folder(s) (t1-live-dev $1 (no run.json: the per-run ceiling), t2-live-test $1"
        " (no run.json: the per-run ceiling)) · ceiling $5 · left $3. This run counts toward it:"
        " no."
    ) in lines[0]
    assert lines[1:] == [
        "Counted at the $1 per-run ceiling, no run.json (a kill, a failed write, or a run still"
        f" going in another terminal): {VERSION_DIR}/t1-live-dev, {VERSION_DIR}/t2-live-test."
        " Once its run has ended, record its real spend in the folder's recorded-spend.json and"
        " commit it (runbook §6).",
        BRANCH_LINE,
    ]


def test_left_subtracts_the_ceiling_and_the_refusal_works_on_it(repo: Repo) -> None:
    # Decided 2026-10-27 (shesha), after zara's Z4: "left" subtracts $1.00 for a folder without
    # run.json, never "at most". By hand: 3.80 + 1.00 = 4.80 spent, 0.20 left.
    repo.folder("t1-live-dev", run_json("t1-live-dev", total="3.80"))
    before = repo.spend()
    repo.folder("t2-live-dev", None, committed=False, extra="llm_call_log.jsonl")  # in progress

    spend = repo.spend()

    assert (before.left, spend.left, spend.lower_bound) == (Decimal("1.20"), Decimal("0.20"), False)
    assert spend_refusal(before, Decimal("1.00")) is None
    assert spend_refusal(spend, Decimal("0.20")) is None
    assert spend_refusal(spend, Decimal("0.21")) == (
        "--max-usd $0.21 is more than what is left of M3's $5 ceiling (D15 (iii)): $4.8 spent in 2"
        " folder(s) (t1-live-dev $3.8, t2-live-dev $1 (no run.json: the per-run ceiling)), left"
        " $0.2. Type a cap within what is left, or record the real spend of a folder counted at"
        " the per-run ceiling in its recorded-spend.json (runbook §6)"
    )


def test_a_committed_recorded_spend_replaces_the_ceiling(repo: Repo) -> None:
    # By hand: 3.80 + 0.42 (recorded) = 4.22 spent, 0.78 left.
    repo.folder("t1-live-dev", run_json("t1-live-dev", total="3.80"))
    repo.folder("t2-live-dev", None, extra="llm_call_log.jsonl")
    repo.note("t2-live-dev", note_json("0.42"))

    spend = repo.spend()

    assert [(f.folder.name, f.total_usd, f.basis, f.why) for f in spend.counted] == [
        ("t1-live-dev", Decimal("3.80"), "run.json", "hybrid (retrieval block)"),
        ("t2-live-dev", Decimal("0.42"), "recorded", f"no run.json: recorded by shesha on {DAY}"),
    ]
    assert (spend.spent, spend.left) == (Decimal("4.22"), Decimal("0.78"))
    assert spend.at_ceiling == () and spend.uncommitted == ()
    text = spend_text(spend, None)
    assert "(t1-live-dev $3.8, t2-live-dev $0.42 (no run.json: recorded))" in text
    assert "per-run ceiling" not in text
    assert spend_refusal(spend, Decimal("0.78")) is None
    refusal = spend_refusal(spend, Decimal("0.79"))
    assert refusal is not None and refusal.endswith("left $0.78. Type a cap within what is left")


def test_a_recorded_spend_that_is_not_m3s_never_counts(repo: Repo) -> None:
    # An M2 run that left no run.json: M2's runs never count (decided 2026-10-27, shesha), so the
    # note says so and the folder leaves the sum.
    repo.folder("t1-live-dev", None)
    repo.note("t1-live-dev", note_json("0.90", m3=False))

    spend = repo.spend()

    assert (spend.counted, spend.spent, spend.uncommitted) == ((), Decimal(0), ())


@pytest.mark.parametrize("case", ["on-disk-only", "changed", "next-to-a-run-json"])
def test_a_recorded_spend_not_committed_as_on_disk_is_refused(repo: Repo, case: str) -> None:
    # Decided 2026-10-27 (shesha): a live folder of M3 not committed as it is refuses the next
    # live run; the sum stays HEAD's (the ceiling, HEAD's note, or HEAD's run.json).
    if case == "next-to-a-run-json":
        repo.folder("t1-live-dev", run_json("t1-live-dev", total="0.30"))
        repo.note("t1-live-dev", note_json("0.10"), committed=False)
    else:
        repo.folder("t1-live-dev", None)
        repo.note("t1-live-dev", note_json("0.42"), committed=case == "changed")
        (repo.root / VERSION_DIR / "t1-live-dev" / RECORDED_SPEND_FILE).write_bytes(
            note_json("0.10")
        )

    spend = repo.spend()

    assert spend.uncommitted == (VERSION_DIR / "t1-live-dev",)
    assert (
        spend.spent
        == {
            "on-disk-only": Decimal("1.00"),
            "changed": Decimal("0.42"),
            "next-to-a-run-json": Decimal("0.30"),
        }[case]
    )
    refusal = spend_refusal(spend, Decimal("0.01"))
    assert refusal is not None
    assert refusal.startswith(
        f"a live folder of M3 is not committed as it is on disk ({VERSION_DIR}"
    )


@pytest.mark.parametrize(
    "content",
    [
        note_json(0.42),  # a JSON number: money is never a float
        note_json("1.01"),  # above any run's cap
        note_json("-0.01"),
        note_json(recorded_by="shesha\\nAUDIENCE LEAK"),  # printed in the plan: no control text
        note_json(recorded_by=""),
        note_json(source=""),
        note_json(extra="field"),
        json.dumps({"total_usd": "0.42", "recorded_by": "shesha"}).encode(),  # fields missing
        b"{not json",
    ],
    ids=[
        "float",
        "above-1",
        "negative",
        "newline-in-name",
        "empty-name",
        "empty-source",
        "extra-field",
        "missing-fields",
        "not-json",
    ],
)
def test_an_invalid_committed_recorded_spend_cannot_be_counted(repo: Repo, content: bytes) -> None:
    repo.folder("t1-live-dev", None)
    repo.note("t1-live-dev", content)

    with pytest.raises(
        SpendError,
        match=r"t1-live-dev/recorded-spend\.json as committed in HEAD is not a valid recorded",
    ):
        repo.spend()


def test_a_recorded_spend_takes_zero_and_the_ceiling_itself() -> None:
    for total in ("0", "1.00"):
        assert RecordedSpend.model_validate_json(note_json(total)).total_usd == Decimal(total)


def test_a_folder_with_both_run_json_and_a_recorded_spend_in_head_cannot_be_counted(
    repo: Repo,
) -> None:
    repo.folder("t1-live-dev", run_json("t1-live-dev", total="0.30"))
    repo.note("t1-live-dev", note_json("0.10"))

    with pytest.raises(SpendError, match=r"has both run\.json and recorded-spend\.json in HEAD"):
        repo.spend()


def test_a_committed_run_json_deleted_on_disk_is_counted_once_from_head(repo: Repo) -> None:
    repo.folder("t1-live-dev", run_json("t1-live-dev", total="0.30"))
    repo.folder("t2-live-dev", run_json("t2-live-dev", total="0.90", config=CONFIG))  # M2's
    for name in ("t1-live-dev", "t2-live-dev"):
        (repo.root / VERSION_DIR / name / RUN_FILE).unlink()

    spend = repo.spend()

    # Never a second time at the ceiling, and M2's folder stays out.
    assert [(f.folder.name, f.total_usd, f.basis) for f in spend.counted] == [
        ("t1-live-dev", Decimal("0.30"), "run.json")
    ]
    assert spend.uncommitted == ()


def test_the_plan_names_the_head_it_read() -> None:
    # Review of PR 5, diego D1: the CLI sets the branch and commit (`read_head_ref`).
    spend = M3Spend(counted=(), uncommitted=(), head="HEAD (branch b at c)")

    text = spend_text(spend, None)
    assert text.startswith(
        "M3 spend so far (D15 (iii); live folders committed in HEAD (branch b at c) that are M3's:"
    )
    # Decided 2026-10-27 (shesha): the plan says where every M3 live run is made, every time.
    assert text.splitlines()[-1] == BRANCH_LINE
    assert M3_LIVE_BRANCH == "m3/close-out" and M3_LIVE_BRANCH in BRANCH_LINE


def test_the_head_ref_is_read_from_the_real_repository_read_only() -> None:
    ref = read_head_ref(REPO)

    # CI checks out a pull request as a detached HEAD; locally it is a branch.
    assert re.fullmatch(r"(branch \S+|a detached HEAD) at [0-9a-f]{7,40}", ref), ref


def test_the_head_ref_outside_a_repository_is_a_git_error(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="git rev-parse --abbrev-ref HEAD"):
        read_head_ref(tmp_path)


def test_an_invalid_committed_run_json_cannot_be_counted(repo: Repo) -> None:
    repo.folder("t1-live-dev", b"{}")

    with pytest.raises(SpendError, match=r"t1-live-dev/run\.json as committed in HEAD is not a"):
        repo.spend()


@pytest.mark.parametrize(
    ("rest", "cap", "refusal"),
    [
        ("4.00", "0.50", None),
        (
            "4.00",
            "0.51",
            "--max-usd $0.51 is more than what is left of M3's $5 ceiling (D15 (iii)): $4.5 spent"
            " in 2 folder(s) (t1-live-dev $0.5, t2-live-dev $4), left $0.5. Type a cap"
            " within what is left",
        ),
        (
            "4.70",
            "0.01",
            "--max-usd $0.01 is more than what is left of M3's $5 ceiling (D15 (iii)): $5.2 spent"
            " in 2 folder(s) (t1-live-dev $0.5, t2-live-dev $4.7), left $0. Type a cap"
            " within what is left",
        ),
    ],
    ids=["exactly-what-is-left", "a-cent-over", "over-the-ceiling"],
)
def test_a_cap_above_what_is_left_is_refused_naming_ceiling_sum_and_folders(
    repo: Repo, rest: str, cap: str, refusal: str | None
) -> None:
    # By hand: 0.50 + 4.00 = 4.50 spent, 0.50 left (a cap of 0.50 fits, 0.51 does not);
    # 0.50 + 4.70 = 5.20, past the ceiling, so nothing is left.
    repo.folder("t1-live-dev", run_json("t1-live-dev", total="0.50"))
    repo.folder("t2-live-dev", run_json("t2-live-dev", total=rest))

    assert spend_refusal(repo.spend(), Decimal(cap)) == refusal


def test_a_runs_spend_counts_only_when_it_is_m3s_and_live() -> None:
    assert m3_spend_reason(run_info(dry_run=False, config=HYBRID_CONFIG)) == (
        "hybrid (retrieval block)"
    )
    assert m3_spend_reason(run_info(dry_run=False, config=CONFIG, m3_spend=True)) == (
        "vector, named by --m3-spend"
    )
    assert m3_spend_reason(run_info(dry_run=False, config=CONFIG)) is None
    assert m3_spend_reason(run_info(dry_run=True, config=HYBRID_CONFIG)) is None


def test_head_paths_are_read_from_the_real_repository_read_only() -> None:
    paths = read_head_paths(REPO, PurePosixPath("evals/datasets/support_qa"))

    assert PurePosixPath("evals/datasets/support_qa/v0.1/manifest.json") in paths
    assert all(path.is_relative_to("evals/datasets/support_qa") for path in paths)


def test_head_paths_outside_a_repository_are_a_git_error(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="git ls-tree HEAD"):
        read_head_paths(tmp_path, PurePosixPath("evals/reports"))

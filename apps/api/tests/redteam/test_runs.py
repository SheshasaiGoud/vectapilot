"""M7 PR 2: `redteam run` (dry) and `summarize` (`redteam/runs.py`, `redteam/summary.py`) on the
real frozen v0.1, with a TEST-ONLY `Io` (no git; the folder goes under tmp_path).

Since H2 (2026-11-20) a dry run scores category E in process; since PR 6 the in-process agent
cases run and reach H1's stub (pending), and every registered database case is "not run".
Stand-in runners (TEST-ONLY, never registered) show the other statuses reach the summary:
passed, missed, over-escalated, not run.
"""

import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from tests.redteam.suite import CASES, DATASET_DIR, GENERATOR
from vectapilot_api.guards.types import DecidedBy
from vectapilot_api.llm_cli import GitInfo
from vectapilot_api.redteam.cases import Category
from vectapilot_api.redteam.harness import FilledCase, RunEnv, Runner
from vectapilot_api.redteam.pii import PLACEHOLDER
from vectapilot_api.redteam.plan import Build, CasePath
from vectapilot_api.redteam.runinfo import (
    DRY_LINE,
    REPLAY_LINE,
    RESULTS_FILE,
    RUN_FILE,
    SUMMARY_MD,
    RedteamRunInfo,
)
from vectapilot_api.redteam.runs import (
    Io,
    Refused,
    run_dry,
    run_one,
    summarize_command,
    write_summary,
)
from vectapilot_api.redteam.scoring import CaseResult
from vectapilot_api.redteam.summary import ResultLine, render, wilson_text

T0 = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


def fake_io(root: Path) -> Io:
    git = GitInfo(root=root, commit="c" * 40, uncommitted_changes=True)
    return Io(git=lambda: git, now=lambda: T0, environment=lambda: "test")


def lines_of(folder: Path) -> list[ResultLine]:
    text = (folder / RESULTS_FILE).read_text("utf-8")
    return [ResultLine.model_validate_json(line) for line in text.splitlines()]


DATABASE_REGISTERED: Final = frozenset(
    {"a-001", "c-001", "c-007", "c-008", "k-001", "k-002", "k-003", "k-004"}
    | {f"f-00{n}" for n in range(1, 7)}
    | {f"j-00{n}" for n in range(1, 9)}
)
"""Every case whose runner is registered (PR 5's limiter; PR 6's agent, logs and search leg) and
that runs in the database environment: F, J, K and the end-to-end cases of the agent's surfaces."""


def test_a_dry_run_runs_the_in_process_cases_and_leaves_the_database_ones(
    tmp_path: Path,
) -> None:
    folder = run_dry(DATASET_DIR, io=fake_io(tmp_path))

    assert folder == tmp_path / "evals/reports/tmp/redteam/20261117T090000Z-fake-redteam"
    lines = lines_of(folder)
    assert [line.id for line in lines] == sorted(CASES)
    # A dry run never has Postgres or Redis: every registered database case is "not run".
    not_run = {line.id: line.reason for line in lines if line.status == "not_run"}
    assert not_run == dict.fromkeys(DATABASE_REGISTERED, "database_environment")
    # H2 is written (2026-11-20): category E runs in process and pure, so a dry run scores it.
    scored = {line.id: line.status for line in lines if line.status not in ("pending", "not_run")}
    assert scored == {c.id: "passed" for c in CASES.values() if c.category is Category.E}
    # M7 PR 6: the in-process agent cases ran through the real guards and reached H1's stub:
    # 80 agent-path cases less the 14 that run in the database (a-001, c-001, c-007, c-008,
    # f-006, j-001 to j-008, k-003) is 66.
    pending = Counter(line.reason for line in lines if line.status == "pending")
    assert pending == {"pending:H1": 66, "pending:PR 8": 28, "pending:PR 7": 5}
    assert (len(not_run), len(scored), pending.total()) == (22, 18, 99)
    info = RedteamRunInfo.model_validate_json((folder / RUN_FILE).read_text("utf-8"))
    assert (info.dry_run, info.cases, info.cost.total_usd) == (True, 139, Decimal(0))
    assert info.cases_reviewed == 0
    fingerprint = info.fingerprint
    assert (fingerprint.provider, fingerprint.agent_loop, fingerprint.rules_version) == (
        "fake",
        "replay",
        None,
    )
    summary = (folder / SUMMARY_MD).read_text("utf-8")
    for expected in (
        DRY_LINE,
        REPLAY_LINE,
        "rules version: no data/safety yet",
        "PII generator: redteam-pii.1, seed 20261116",
        "- pending: 99",
        "  - pending:H1: 66 (a-002,",
        "- not run: a-001, c-001, c-007, c-008, f-001,",
        "with uncommitted changes). CI gate: not checked by this run",
        "Crisis messages: not measured",
        # The plan's "Not in M7": the MCP server's results are a listed gap (leo's PR 6 review).
        "MCP results: not screened (known_gap:mcp_unscreened;",
        "- hard passed: 18 of 18 scored (94 not scored)",  # category E, since H2
        "| mask-several-types | 2 | 0 | 0 |",
        # noah's review: nothing scored reads "not measured", never a zero.
        "- known gaps still failing as listed: not measured (0 of 13 scored)",
        "Leaks: not measured (0 of 10 scored).",
        "Synthetic, agent-drafted (zara-safety): 0 of 139 cases reviewed by a person.",
        "## Safety recall by family (G-in and H-req, hard cases)",
    ):
        assert expected in summary
    assert "Leaks: 0" not in summary


def test_the_run_folder_holds_no_case_text_and_no_generated_value(tmp_path: Path) -> None:
    folder = run_dry(DATASET_DIR, io=fake_io(tmp_path))
    written = "".join(p.read_text("utf-8") for p in folder.iterdir())

    texts = [c.input for c in CASES.values() if len(c.input) >= 20]
    values = [
        GENERATOR.fill(c.id, m.group(0))
        for c in CASES.values()
        for m in PLACEHOLDER.finditer(c.input)
    ]
    assert len(texts) > 100 and len(values) > 20
    assert not [t for t in texts if t in written]
    assert not [v for v in values if v in written]


def stand_in(filled: FilledCase, env: RunEnv) -> CaseResult:
    """TEST-ONLY: a red flag hits; a soft look-alike over-escalates; everything else lets the
    message through (so most hard cases miss)."""
    case = filled.case
    if case.expect == "urgent_handoff":
        return CaseResult(case.id, "urgent_handoff", DecidedBy.RED_FLAG_EMERGENCY)
    if case.id in ("l-009", "l-014"):
        return CaseResult(
            case.id, "write_tools_removed", DecidedBy.INJECTION_MESSAGE, model_calls=1
        )
    return CaseResult(case.id, "allowed", DecidedBy.NONE, reached=True, model_calls=1)


def test_scored_cases_reach_the_summary_with_their_statuses(tmp_path: Path) -> None:
    runners: dict[CasePath, Runner] = {CasePath.AGENT: stand_in}
    written = {Build.H1: lambda: True, Build.H2: lambda: True, Build.H3: lambda: True}

    folder = run_dry(DATASET_DIR, io=fake_io(tmp_path), runners=runners, probes=written)

    lines = lines_of(folder)
    status = {line.id: line.status for line in lines}
    assert status["g-001"] == "passed"
    assert status["g-007"] == "passed"  # a known gap that "passes" here: the gate fails it
    assert status["l-009"] == status["l-014"] == "over_escalated"
    assert status["l-005"] == "passed"
    assert status["a-002"] == "missed"
    assert status["a-001"] == status["j-001"] == "not_run"  # the database environment
    assert status["b-001"] == "pending"
    summary = (folder / SUMMARY_MD).read_text("utf-8")
    soft = [ln for ln in lines if ln.label == "soft" and ln.status != "pending"]
    assert len(soft) == 7  # the agent-route look-alikes; the answer-route ones wait for PR 8
    assert f"{wilson_text(2, 7)} over-escalated" in summary
    assert "| sunrise-clinic | en |" in summary


def test_summarize_rewrites_the_summary_and_refuses_a_foreign_folder(tmp_path: Path) -> None:
    folder = run_dry(DATASET_DIR, io=fake_io(tmp_path))
    (folder / SUMMARY_MD).write_text("stale", "utf-8")

    assert summarize_command(folder) == 0
    assert DRY_LINE in (folder / SUMMARY_MD).read_text("utf-8")
    (folder / RUN_FILE).write_text(json.dumps({"run_id": "x"}), "utf-8")
    with pytest.raises(Refused, match="not a red-team run's"):
        write_summary(folder)


def test_wilson_text_is_the_existing_interval() -> None:
    assert wilson_text(0, 0) == "0 of 0 (no interval)"
    assert wilson_text(1, 1) == "1 of 1 (20.7 % to 100.0 %)"
    assert sum(1 for c in CASES.values() if c.category is Category.L and c.id >= "l-007") == 14


def test_a_missed_line_keeps_what_the_case_got_and_a_leak_is_counted(tmp_path: Path) -> None:
    def leaking(filled: FilledCase, env: RunEnv) -> CaseResult:
        """TEST-ONLY: every case gets an urgent hand-off by the wrong rule."""
        return CaseResult(
            filled.case.id, "urgent_handoff", DecidedBy.GUARD_ERROR, handoff_reason="guard_error"
        )

    written = {Build.H1: lambda: True, Build.H2: lambda: True, Build.H3: lambda: True}
    env = RunEnv(GENERATOR, "in_process")
    line = run_one(
        CASES["l-006"], "hard", env, frozenset(), runners={CasePath.AGENT: leaking}, probes=written
    )

    assert (line.status, line.outcome, line.decided_by, line.handoff_reason) == (
        "missed",
        "urgent_handoff",
        "guard_error",
        "guard_error",
    )
    # A leak is always a miss (expect no_leak): counted from the missed line's outcome.
    folder = run_dry(DATASET_DIR, io=fake_io(tmp_path))
    lines = [
        ln.model_copy(update={"status": "missed", "outcome": "leak"}) if ln.id == "k-002" else ln
        for ln in lines_of(folder)
    ]
    info = RedteamRunInfo.model_validate_json((folder / RUN_FILE).read_text("utf-8"))
    assert "Leaks: 1 (k-002) in 1 scored; 9 not scored." in render(info, lines)

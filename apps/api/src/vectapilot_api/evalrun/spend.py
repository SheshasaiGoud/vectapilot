"""M3's spend across its live runs (M3 PR 5; D15 (iii), ADR 0013 amendment 2026-10-23).

Every paid run of M3 counts towards one ceiling of $5.00, and each run still keeps M1's cap of at
most $1.00 (`--max-usd`). Before a live run's cap prompt, `cli.run_command` prints three figures
from this module and refuses a cap above what is left. Like the typed cap, this only catches
mistakes: the providers' own spend limits stay the real cap across runs (ADR 0007).

**What counts** (decided 2026-10-27, shesha): every live run folder committed in `HEAD`
(`evals/reports/<dataset>/<version>/<UTC start>-live-<split>/`) that is M3's
(`report.m3_spend_reason`): its run.json has a `retrieval` block (every hybrid run is M3's), or
it is a vector run named with `--m3-spend` (D3's re-run after a price change: a vector run has no
block, so it names itself when it runs, in its own run.json). M2's runs never count. The sum is
of `cost.total_usd` as committed in `HEAD`, never as on disk: only committed folders count, so
each live folder is committed before the next live run.

**A live folder without run.json** (a kill, a failed write after the calls, or a run still going
in another terminal: run.json is written last) cannot say whose run it was or what it spent. It
counts at M1's $1.00 per-run ceiling (`MAX_USD_CEILING`: no run's cap is above it, so no run spent
more), until shesha records its real spend in that folder's `recorded-spend.json`
(`RecordedSpend`) and commits it (decided 2026-10-27, shesha; runbook §6). "Left" subtracts it
either way, and the refusal works on that.

**What it cannot see**, and says so:
- an answer-model cost that was incomplete in a counted folder (`llm_cost_complete` false): the
  sum is then a lower bound, and the plan says "lower bound" and "left ... at most";
- a live folder committed on another branch only: HEAD is the current branch's, so the plan names
  the branch and commit it read (`read_head_ref`; review of PR 5, diego D1), and every M3 live
  run is made, and its folder committed, on one branch (`M3_LIVE_BRANCH`, decided 2026-10-27,
  shesha; runbook §6).

**What it refuses** (before a counted live run, before the cap prompt; `cli.run_command`):
- a live folder on disk that would count but whose run.json, or any recorded-spend.json, is not
  in `HEAD` byte for byte (not committed yet, or changed since; decided 2026-10-27, shesha);
- a typed cap above what is left; the refusal names the ceiling, the sum and the folders.

**One counting core, one set of rules per milestone** (M4 PR 4). The rules above are M3's
(`M3_RULES`); `count_spend` applies any `SpendRules`: which folders are live (a pattern), the
ceiling, the one branch, and which run is the milestone's and why (a reason read from run.json).
M4 counts its own live folders against its own ceiling with the same core
(`intenteval/spend.py`, D7); M3's figures, texts and tests do not change, and M4's folder names
never match M3's `LIVE_FOLDER` (tested), so neither milestone's runs can block the other's.

Read-only: `git ls-tree`, `git cat-file` (`llm_cli.read_head_file`) and `git rev-parse`, never a
write.
"""

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vectapilot_api.evalrun.budget import usd
from vectapilot_api.evalrun.report import RUN_FILE, RunInfo, m3_spend_reason
from vectapilot_api.gateway.types import Usd
from vectapilot_api.llm_cli import REPORTS_DIR, GitError, _git_executable
from vectapilot_api.measure.budget import MAX_USD_CEILING

M3_CEILING_USD: Final = Decimal("5.00")
"""D15 (iii), decided 2026-10-23 by shesha: all of M3's paid runs together."""

M3_LIVE_BRANCH: Final = "m3/close-out"
"""The one branch every M3 live run is made and committed on (decided 2026-10-27, shesha): the
sum reads the current branch's HEAD only."""

RECORDED_SPEND_FILE: Final = "recorded-spend.json"
"""A live folder's real spend, recorded by hand when it has no run.json (`RecordedSpend`)."""

LIVE_FOLDER: Final = re.compile(r"^evals/reports/[^/]+/[^/]+/[^/]+-live-(?:dev|test)$")
"""A live eval run folder, relative to the repository root (`cli._run_dir`)."""

HeadPaths = Callable[[Path, PurePosixPath], Sequence[PurePosixPath]]
"""(repository root, a directory relative to it) → every file under it in HEAD."""
HeadFile = Callable[[Path, PurePosixPath], bytes | None]

Basis = Literal["run.json", "recorded", "ceiling"]
"""Where a counted folder's figure comes from: its run.json; its recorded-spend.json (no
run.json); or the $1.00 per-run ceiling (no run.json and nothing recorded yet)."""


class SpendError(Exception):
    """M3's spend cannot be counted. The message names paths, never file content."""


class RecordedSpend(BaseModel):
    """`recorded-spend.json`: the real spend of a live folder that has no run.json, written by
    shesha after the run ended and committed in that folder (decided 2026-10-27, shesha; runbook
    §6). Read from HEAD only, like run.json; until it is committed, the folder counts at the
    $1.00 per-run ceiling. Example:

        {"total_usd": "0.4213", "counts_toward_m3": true, "recorded_by": "shesha",
         "recorded_on": "2026-11-02", "source": "the printed counted spend and llm_call_log.jsonl"}
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    total_usd: Usd = Field(le=MAX_USD_CEILING)
    """What the run spent, as a decimal string (never a JSON number: `Usd` refuses a float). At
    most M1's per-run ceiling: no run's cap is above it, so a larger figure is a typo."""
    counts_toward_m3: bool
    """False only for a run that was not M3's (an M2 run that left no run.json): M2's runs never
    count (decided 2026-10-27, shesha). Required, so the note always says which. M4's rules do
    not read it: every folder M4's pattern matches is M4's by its name (`intenteval/spend.py`)."""
    recorded_by: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9 ._-]{0,39}$")
    """Who recorded it (printed in the plan, so letters, digits, spaces, `.`, `_` and `-` only)."""
    recorded_on: date
    source: str = Field(min_length=1, max_length=300)
    """Where the figure was read: the run's printed spend line, its record logs. Never printed."""


# ── The rules of one milestone ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunSpend:
    """What one committed run.json says about its spend: the total, whether the answer-model cost
    was complete, and why it counts (None: the run is not this milestone's)."""

    total_usd: Decimal
    cost_complete: bool
    why: str | None


RunReason = Callable[[bytes], RunSpend | None]
"""A run.json's bytes → its `RunSpend`, or None when they are not a valid run.json."""


@dataclass(frozen=True)
class SpendRules:
    """Whose live folders count, against what (the module docstring, "One counting core")."""

    milestone: str
    """`M3`, `M4`: printed in every line."""
    decision: str
    """The decision that set the ceiling, e.g. `D15 (iii)`."""
    ceiling: Decimal
    branch: str
    branch_decided: str
    """Where the one-branch rule comes from, e.g. `decided 2026-10-27`."""
    folder: re.Pattern[str]
    """A live folder, relative to the repository root."""
    disk_glob: str
    """The same folders on disk, as a glob under `evals/reports/` (then matched by `folder`)."""
    run_reason: RunReason
    note_counts: Callable[[RecordedSpend], bool]
    """Whether a committed recorded-spend.json counts toward this milestone."""
    what_counts: str
    """The plan's words for what counts, e.g. `hybrid, named by --m3-spend, or without run.json`."""

    @property
    def branch_line(self) -> str:
        return (
            f"Make every {self.milestone} live run, and commit its folder, on {self.branch}"
            f" ({self.branch_decided}): this sum reads the current branch's HEAD only, so a"
            " folder committed on another branch is not counted."
        )


def _m3_run_reason(content: bytes) -> RunSpend | None:
    try:
        info = RunInfo.model_validate_json(content)
    except ValidationError:
        return None
    return RunSpend(info.cost.total_usd, info.cost.llm_cost_complete, m3_spend_reason(info))


def _m3_note_counts(note: RecordedSpend) -> bool:
    return note.counts_toward_m3


M3_RULES: Final = SpendRules(
    milestone="M3",
    decision="D15 (iii)",
    ceiling=M3_CEILING_USD,
    branch=M3_LIVE_BRANCH,
    branch_decided="decided 2026-10-27",
    folder=LIVE_FOLDER,
    disk_glob="*/*/*-live-*",
    run_reason=_m3_run_reason,
    note_counts=_m3_note_counts,
    what_counts="hybrid, named by --m3-spend, or without run.json",
)


def read_head_paths(root: Path, under: PurePosixPath) -> list[PurePosixPath]:
    """Every file under `under` (relative to the repository root) as committed in HEAD
    (read-only: `git ls-tree`). Raises `GitError` when git cannot list it."""
    # Fixed arguments plus one path argument, no shell (S603: nothing here is parsed by a shell).
    done = subprocess.run(  # noqa: S603
        [
            _git_executable(),
            "-C",
            str(root),
            "ls-tree",
            "-r",
            "--name-only",
            "--full-name",
            "-z",
            "HEAD",
            "--",
            under.as_posix(),
        ],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        raise GitError(f"`git ls-tree HEAD -- {under}` failed: run inside the repository")
    return [PurePosixPath(name) for name in done.stdout.decode("utf-8").split("\0") if name]


def _rev_parse(root: Path, *args: str) -> str:
    # Fixed arguments, no shell (S603: nothing here is parsed by a shell).
    done = subprocess.run(  # noqa: S603
        [_git_executable(), "-C", str(root), "rev-parse", *args],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        raise GitError(f"`git rev-parse {' '.join(args)}` failed: run inside the repository")
    return done.stdout.decode("utf-8").strip()


def read_head_ref(root: Path) -> str:
    """Which HEAD M3's spend was read from, for the plan: "branch <name> at <short sha>", or "a
    detached HEAD at <short sha>" (read-only: `git rev-parse --abbrev-ref HEAD` and `--short
    HEAD`). Raises `GitError` when git cannot read it."""
    branch = _rev_parse(root, "--abbrev-ref", "HEAD")
    commit = _rev_parse(root, "--short", "HEAD")
    return f"a detached HEAD at {commit}" if branch == "HEAD" else f"branch {branch} at {commit}"


@dataclass(frozen=True)
class CountedFolder:
    folder: PurePosixPath
    total_usd: Decimal
    cost_complete: bool
    why: str
    basis: Basis = "run.json"


@dataclass(frozen=True)
class MilestoneSpend:
    """What a milestone has spent so far, from the committed live folders (module docstring)."""

    counted: tuple[CountedFolder, ...]
    uncommitted: tuple[PurePosixPath, ...]
    """Live folders on disk that would count but whose run.json or recorded-spend.json is not as
    committed in HEAD."""
    rules: SpendRules = M3_RULES
    head: str = "HEAD"
    """Which HEAD was read, for the plan: the CLI sets `read_head_ref`'s "branch <name> at <short
    sha>" (review of PR 5, diego D1)."""

    @property
    def ceiling(self) -> Decimal:
        return self.rules.ceiling

    @property
    def spent(self) -> Decimal:
        return sum((folder.total_usd for folder in self.counted), Decimal(0))

    @property
    def lower_bound(self) -> bool:
        """True when a counted folder's answer-model cost was incomplete: "left" is then at most."""
        return any(not folder.cost_complete for folder in self.counted)

    @property
    def left(self) -> Decimal:
        return max(self.ceiling - self.spent, Decimal(0))

    @property
    def at_ceiling(self) -> tuple[PurePosixPath, ...]:
        """Live folders without run.json counted at the per-run ceiling (nothing recorded yet)."""
        return tuple(f.folder for f in self.counted if f.basis == "ceiling")


M3Spend = MilestoneSpend
"""M3's name for it (M3 PR 5), kept: its default rules are M3's."""


def _parse_note(content: bytes) -> RecordedSpend | None:
    try:
        return RecordedSpend.model_validate_json(content)
    except ValidationError:
        return None


CEILING_WHY: Final = (
    f"no run.json: counted at the {usd(MAX_USD_CEILING)} per-run ceiling until its spend is"
    f" recorded in {RECORDED_SPEND_FILE}"
)


def _at_ceiling(folder: PurePosixPath) -> CountedFolder:
    return CountedFolder(folder, MAX_USD_CEILING, True, CEILING_WHY, "ceiling")


def _counted_in_head(
    root: Path,
    folder: PurePosixPath,
    committed: set[PurePosixPath],
    head_file: HeadFile,
    rules: SpendRules,
) -> CountedFolder | None:
    """A live folder as committed in HEAD: counted from its run.json (None: not the milestone's),
    else from its recorded-spend.json (None: recorded as not the milestone's), else at the
    per-run ceiling."""
    has_run, has_note = folder / RUN_FILE in committed, folder / RECORDED_SPEND_FILE in committed
    if has_run and has_note:
        raise SpendError(
            f"{folder} has both {RUN_FILE} and {RECORDED_SPEND_FILE} in HEAD: a folder with"
            f" {RUN_FILE} is counted from it, so remove the note before a live run"
        )
    if has_run:
        spend = rules.run_reason(head_file(root, folder / RUN_FILE) or b"")
        if spend is None:
            raise SpendError(
                f"{folder / RUN_FILE} as committed in HEAD is not a valid run.json, so its spend"
                " cannot be counted: fix it before a live run"
            )
        if spend.why is None:
            return None
        return CountedFolder(folder, spend.total_usd, spend.cost_complete, spend.why)
    if has_note:
        note = _parse_note(head_file(root, folder / RECORDED_SPEND_FILE) or b"")
        if note is None:
            raise SpendError(
                f"{folder / RECORDED_SPEND_FILE} as committed in HEAD is not a valid recorded"
                " spend (runbook §6), so its spend cannot be counted: fix it before a live run"
            )
        if not rules.note_counts(note):
            return None
        why = f"no run.json: recorded by {note.recorded_by} on {note.recorded_on.isoformat()}"
        return CountedFolder(folder, note.total_usd, True, why, "recorded")
    return _at_ceiling(folder)


def count_spend(
    root: Path, rules: SpendRules, head_paths: HeadPaths, head_file: HeadFile
) -> MilestoneSpend:
    """Count one milestone's spend from HEAD by its `rules`, and find what is on disk but not
    committed (module docstring). Raises `SpendError` for a committed live run.json or
    recorded-spend.json that cannot be read, or a folder with both: its spend would be missed."""
    committed = set(head_paths(root, PurePosixPath(REPORTS_DIR.as_posix())))
    head_folders = {path.parent for path in committed if rules.folder.match(path.parent.as_posix())}
    counted: dict[PurePosixPath, CountedFolder] = {}
    for folder in sorted(head_folders):
        entry = _counted_in_head(root, folder, committed, head_file, rules)
        if entry is not None:
            counted[folder] = entry
    uncommitted: list[PurePosixPath] = []
    for path in sorted((root / REPORTS_DIR).glob(rules.disk_glob)):
        folder = PurePosixPath(path.relative_to(root).as_posix())
        if not path.is_dir() or not rules.folder.match(folder.as_posix()):
            continue
        note = path / RECORDED_SPEND_FILE
        note_changed = note.is_file() and (
            note.read_bytes() != head_file(root, folder / RECORDED_SPEND_FILE)
        )
        run_json = path / RUN_FILE
        if not run_json.is_file():
            if folder not in head_folders:  # on disk only, nothing committed: the ceiling
                counted[folder] = _at_ceiling(folder)
            if note_changed:
                uncommitted.append(folder)
            continue
        content = run_json.read_bytes()
        if head_file(root, folder / RUN_FILE) == content and not note_changed:
            continue
        spend = rules.run_reason(content)
        if note_changed or spend is None or spend.why is not None:
            uncommitted.append(folder)  # unreadable: it may be the milestone's
    return MilestoneSpend(
        counted=tuple(counted[folder] for folder in sorted(counted)),
        uncommitted=tuple(uncommitted),
        rules=rules,
    )


def m3_spend(root: Path, head_paths: HeadPaths, head_file: HeadFile) -> MilestoneSpend:
    """Count M3's spend from HEAD (`count_spend` with `M3_RULES`)."""
    return count_spend(root, M3_RULES, head_paths, head_file)


_BASIS_NOTE: Final[dict[Basis, str]] = {
    "run.json": "",
    "recorded": " (no run.json: recorded)",
    "ceiling": " (no run.json: the per-run ceiling)",
}


def _folders(spend: MilestoneSpend) -> str:
    return (
        ", ".join(
            f"{folder.folder.name} {usd(folder.total_usd)}{_BASIS_NOTE[folder.basis]}"
            for folder in spend.counted
        )
        or "none"
    )


def _left(spend: MilestoneSpend) -> str:
    """What is left, with "at most" when a counted folder's cost was a lower bound."""
    return f"left {usd(spend.left)}{' at most' if spend.lower_bound else ''}"


BRANCH_LINE: Final = M3_RULES.branch_line


def spend_text(spend: MilestoneSpend, why: str | None) -> str:
    """The plan's "<milestone> spend so far" lines: which HEAD was read, the sum, the ceiling and
    what is left, the counted folders, whether this run counts, what is counted at the per-run
    ceiling, what is not committed, and the one branch for the milestone's live runs."""
    rules = spend.rules
    m = rules.milestone
    bound = " (lower bound: a counted folder's answer-model cost was incomplete)"
    lines = [
        f"{m} spend so far ({rules.decision}; live folders committed in {spend.head} that are"
        f" {m}'s: {rules.what_counts}): {usd(spend.spent)}"
        f"{bound if spend.lower_bound else ''} in"
        f" {len(spend.counted)} folder(s) ({_folders(spend)}) · ceiling {usd(spend.ceiling)} ·"
        f" {_left(spend)}. This run counts toward it: {'no' if why is None else f'yes ({why})'}."
        " Like the cap, this only catches mistakes: the providers' own limits are the real cap"
        " (ADR 0007)."
    ]
    if spend.at_ceiling:
        lines.append(
            f"Counted at the {usd(MAX_USD_CEILING)} per-run ceiling, no run.json (a kill, a failed"
            " write, or a run still going in another terminal): "
            + ", ".join(str(folder) for folder in spend.at_ceiling)
            + f". Once its run has ended, record its real spend in the folder's"
            f" {RECORDED_SPEND_FILE} and commit it (runbook §6)."
        )
    if spend.uncommitted:
        lines.append(
            f"Not committed as on disk (a live run of {m} is refused until it is): "
            + ", ".join(str(folder) for folder in spend.uncommitted)
        )
    lines.append(rules.branch_line)
    return "\n".join(lines)


def spend_refusal(spend: MilestoneSpend, cap: Decimal) -> str | None:
    """Why a counted live run with this typed cap is refused (None: it may go on to the prompt):
    a counted folder not committed as on disk, or a cap above what is left."""
    rules = spend.rules
    m = rules.milestone
    if spend.uncommitted:
        return (
            f"a live folder of {m} is not committed as it is on disk ("
            + ", ".join(str(folder) for folder in spend.uncommitted)
            + f"): {m}'s spend counts committed folders only ({rules.decision}), so commit each"
            " live folder before the next live run"
        )
    if cap > spend.left:
        record = (
            f", or record the real spend of a folder counted at the per-run ceiling in its"
            f" {RECORDED_SPEND_FILE} (runbook §6)"
            if spend.at_ceiling
            else ""
        )
        return (
            f"--max-usd {usd(cap)} is more than what is left of {m}'s {usd(spend.ceiling)} ceiling"
            f" ({rules.decision}): {usd(spend.spent)}"
            + (" (a lower bound)" if spend.lower_bound else "")
            + f" spent in {len(spend.counted)} folder(s) ({_folders(spend)}), {_left(spend)}."
            f" Type a cap within what is left{record}"
        )
    return None

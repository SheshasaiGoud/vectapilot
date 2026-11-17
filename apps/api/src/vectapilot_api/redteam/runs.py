"""`run` and `summarize` (M7 PR 2): every frozen case through `run_case`, the scorer pytest uses,
into a run folder (`runinfo`).

**`run` is a dry run** ($0, no network, no model): the dry folder
`evals/reports/tmp/redteam/<UTC>-fake-redteam/` (git-ignored) gets `results.jsonl` (one line of
codes per case, `summary.ResultLine`), `run.json` (`runinfo.RedteamRunInfo`, the fingerprint) and
`summary.md`. It needs a frozen version that passes `check`. It runs the in-process cases only:
a case whose runner needs the database (F, J, K and one end-to-end case per surface,
`plan.environment_of`) is reported `not_run` with the reason `database_environment`, because
those run in CI's integration suite (`tests/redteam/test_cases.py`), never against shesha's
local stack from here [proposal]. In PR 2 no guard is built, so every case is `pending`.

**A live run is refused in M7 PR 2:** D7's raw arm (the input screens bypassed in the harness only,
one model call per model-facing case) belongs to the close-out (plan PR 9), after M5's H1 and
the Anthropic key, and is shesha's alone. Agents never pass `--live`.

`summarize RUN_DIR` rewrites a folder's `summary.md` from its `run.json` and `results.jsonl`.
"""

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from vectapilot_api.agent.prompt import AGENT_PROMPT_VERSION
from vectapilot_api.answer.prompt import PROMPT_VERSION
from vectapilot_api.evalrun.report import CostInfo
from vectapilot_api.evalset.corpus import CorpusError
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.guards.config import GuardConfigError, load_guard_dir
from vectapilot_api.llm_cli import GitInfo, read_git
from vectapilot_api.redteam.cases import Case, Label
from vectapilot_api.redteam.checks import RedteamDataset, check_dataset, generator_of
from vectapilot_api.redteam.files import DEFAULT_DATASET, read_dataset
from vectapilot_api.redteam.freeze import file_sha256
from vectapilot_api.redteam.harness import RUNNERS, RunEnv, Runner, run_case
from vectapilot_api.redteam.pii import GENERATOR_VERSION
from vectapilot_api.redteam.plan import (
    HAND_WRITE_PROBES,
    Build,
    CasePath,
    end_to_end_ids,
    environment_of,
    path_of,
)
from vectapilot_api.redteam.runinfo import (
    RESULTS_FILE,
    RUN_FILE,
    SUMMARY_MD,
    Fingerprint,
    RedteamRunInfo,
    folder_name,
    run_dir,
)
from vectapilot_api.redteam.scoring import (
    ExpectationMiss,
    NotScored,
    PendingHandWrite,
    Verdict,
)
from vectapilot_api.redteam.summary import ResultLine, Status, render

PROG: Final = "python -m vectapilot_api.redteam"
LIVE_REFUSED: Final = (
    "--live is refused: D7's raw-arm live run is built in the close-out (plan PR 9), after M5's"
    " H1 and the Anthropic key, and is shesha's alone"
)
SAFETY_DIR: Final = Path("data", "safety")
CORPUS_DIR: Final = Path("data", "sample")
HARNESS_SWITCH: Final = "on"
"""The harness tests the guards' code: both switches on (summary's `ANSWER_NOTE`)."""


class Refused(Exception):
    """The command cannot run as asked. The message names ids and rules, never text."""


def _environment() -> str:
    return os.environ.get("VECTAPILOT_ENVIRONMENT", "local")


@dataclass(frozen=True)
class Io:
    """What the commands read from outside: tests pass their own, so no test runs git."""

    git: Callable[[], GitInfo] = read_git
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    environment: Callable[[], str] = _environment


def rules_version(root: Path) -> str | None:
    """The SHA-256 of `data/safety/` as the guards' loader computes it; None before PR 4."""
    folder = root / SAFETY_DIR
    if not folder.is_dir():
        return None
    try:
        return load_guard_dir(folder, root / CORPUS_DIR).rules_version
    except GuardConfigError as error:
        raise Refused(f"data/safety does not load: {error}") from None


def make_fingerprint(root: Path, dataset: RedteamDataset, dataset_dir: Path) -> Fingerprint:
    freeze = (dataset_dir / "freeze.json").read_bytes()
    catalog = load_catalog()
    seed = generator_of(dataset).seed
    return Fingerprint(
        dataset_version=dataset.manifest.version,
        freeze_sha256=hashlib.sha256(freeze).hexdigest(),
        gates_sha256=file_sha256(dataset.gates_bytes),
        rules_version=rules_version(root),
        pii_generator=GENERATOR_VERSION,
        pii_seed=seed,
        guard_routes=HARNESS_SWITCH,
        guard_answer=HARNESS_SWITCH,
        provider="fake",
        models={alias: spec.model for alias, spec in catalog.aliases.items()},
        prompt_version=PROMPT_VERSION,
        agent_prompt_version=AGENT_PROMPT_VERSION,
        agent_loop="replay",
    )


def _line(case: Case, label: Label, status: Status, **codes: str | None) -> ResultLine:
    return ResultLine(
        id=case.id,
        subgroup=case.subgroup,
        family=case.family,
        business_id=case.business_id,
        language_row=case.language_row,
        expect=case.expect,
        label=label,
        status=status,
        **codes,
    )


def run_one(
    case: Case,
    label: Label,
    env: RunEnv,
    end_to_end: frozenset[str],
    *,
    runners: Mapping[CasePath, Runner] = RUNNERS,
    probes: Mapping[Build, Callable[[], bool]] = HAND_WRITE_PROBES,
) -> ResultLine:
    """One case's result line (`run_case`'s exceptions become statuses, codes only)."""
    if path_of(case) in runners and environment_of(case, end_to_end) == "database":
        return _line(case, label, "not_run", reason="database_environment")
    try:
        scored = run_case(case, label, env, runners=runners, probes=probes)
    except PendingHandWrite as pending:
        return _line(case, label, "pending", reason=pending.reason)
    except NotScored as not_scored:
        return _line(case, label, "not_scored", reason=not_scored.why)
    except ExpectationMiss as miss:
        decided = None if miss.decided_by is None else miss.decided_by.value
        return _line(
            case,
            label,
            "missed",
            outcome=miss.outcome,
            decided_by=decided,
            handoff_reason=miss.handoff_reason,
            reason="expectation_miss",
        )
    result = scored.result
    status: Status = "passed" if scored.verdict is Verdict.PASSED else "over_escalated"
    decided = None if result.decided_by is None else result.decided_by.value
    return _line(case, label, status, outcome=result.outcome, decided_by=decided)


def _load(dataset_dir: Path, corpus_dir: Path | None) -> RedteamDataset:
    dataset = read_dataset(dataset_dir, corpus_dir)
    if dataset.manifest.stage != "frozen":
        raise Refused(f"{dataset.manifest.version} is not frozen: run `{PROG} freeze` first")
    problems = check_dataset(dataset).problems
    if problems:
        raise Refused(f"{len(problems)} problem(s): run `{PROG} check`")
    return dataset


def write_summary(folder: Path) -> Path:
    """(Re)write `summary.md` from the folder's run.json and results.jsonl."""
    try:
        info = RedteamRunInfo.model_validate_json((folder / RUN_FILE).read_text("utf-8"))
        lines = [
            ResultLine.model_validate_json(text)
            for text in (folder / RESULTS_FILE).read_text("utf-8").splitlines()
            if text.strip()
        ]
    except ValidationError:
        raise Refused(f"{RUN_FILE} or {RESULTS_FILE} is not a red-team run's") from None
    path = folder / SUMMARY_MD
    path.write_text(render(info, lines), "utf-8")
    return path


def run_dry(
    dataset_dir: Path,
    *,
    io: Io,
    corpus_dir: Path | None = None,
    runners: Mapping[CasePath, Runner] = RUNNERS,
    probes: Mapping[Build, Callable[[], bool]] = HAND_WRITE_PROBES,
) -> Path:
    """Every frozen case, in process; returns the run folder (module docstring)."""
    dataset = _load(dataset_dir, corpus_dir)
    git = io.git()
    fingerprint = make_fingerprint(git.root, dataset, dataset_dir)
    started = io.now()
    name = folder_name(live=False, started=started)
    out = run_dir(git.root, dataset.manifest.version, name, live=False)
    out.mkdir(parents=True, exist_ok=False)
    env = RunEnv(generator_of(dataset), "in_process")
    end_to_end = end_to_end_ids(dataset.cases)
    labels = dataset.gates.labels
    lines = [
        run_one(case, labels[case.id], env, end_to_end, runners=runners, probes=probes)
        for case in sorted(dataset.cases, key=lambda c: c.id)
    ]
    (out / RESULTS_FILE).write_text(
        "".join(line.model_dump_json() + "\n" for line in lines), "utf-8"
    )
    info = RedteamRunInfo(
        run_id=name,
        status="complete",
        dry_run=True,
        environment=io.environment(),
        dataset="redteam",
        dataset_version=dataset.manifest.version,
        dataset_stage="frozen",
        cases=len(lines),
        cases_reviewed=sum(1 for case in dataset.cases if case.reviewed_by),
        commit=git.commit,
        uncommitted_changes=git.uncommitted_changes,
        started_at=started,
        ended_at=io.now(),
        fingerprint=fingerprint,
        fingerprint_sha256=fingerprint.sha256(),
        cost=CostInfo(
            cap_usd=None,
            embedding_usd=Decimal(0),
            llm_usd=Decimal(0),
            total_usd=Decimal(0),
            llm_cost_complete=True,
            embedding_requests=0,
            llm_calls=0,
        ),
    )
    (out / RUN_FILE).write_text(info.model_dump_json(indent=2) + "\n", "utf-8")
    write_summary(out)
    return out


def run_command(
    *,
    dataset: Path | None,
    live: bool,
    max_usd: Decimal | None,
    corpus: Path | None = None,
    io: Io | None = None,
) -> int:
    """`run`: refused with `--live` (module docstring); otherwise a dry run."""
    del max_usd
    if live:
        raise Refused(LIVE_REFUSED)
    io = io or Io()
    dataset_dir = dataset or io.git().root / DEFAULT_DATASET
    try:
        out = run_dry(dataset_dir, io=io, corpus_dir=corpus)
    except (DatasetError, CorpusError) as error:
        raise Refused(str(error)) from None
    print(f"dry run complete. Written to {out} (DRY RUN: git ignores it).")
    return 0


def summarize_command(run_dir_: Path) -> int:
    path = write_summary(run_dir_)
    print(f"summary written to {path}")
    return 0

"""`run_case`: one red-team case through its guard, scored (M7 PR 2; scoring rules, "One scorer,
two callers").

pytest (`tests/redteam/test_cases.py`, test id `test_case[<id>]`) and `redteam run` both call
`run_case`, so a verdict means the same in CI and in a summary.

**The registry.** `RUNNERS` maps a `plan.CasePath` to the runner a later PR registers with
`register` (PR 3 masking, PR 5 the limiter, PR 6 the agent, the logs and the search leg, PR 7
documents, PR 8 the answer path). A case whose path has no runner raises `PendingHandWrite` with
`pending:<PR n>` before anything runs (plan, "Design points left to their PRs"). PR 3 registers
the masking path (`masking.run_masking`, category E against H2's `mask_pii`), PR 5 the limiter
path (`limiter.run_limiter`, k-002 and k-004 against real Redis), PR 6 the agent path
(`agent.run_agent`), the log path (`logs.run_logs`) and the search leg (`search.run_search_leg`,
k-001). A runner that reports `guard_error` with `NotImplementedError`
reached a `[HAND-WRITE]` stub: pending on the first unwritten hand-write the case needs
(`plan.unwritten`). If none is unwritten, that hand-off is a plain miss.

**A database case without its served app** (M7 PR 6) [proposal]: a case whose environment is
`database` but whose `RunEnv` has no app (a unit run, where the suite requests the served app only
for cases it will score) is pending on its first unwritten hand-write, probed, without running:
the served app needs Postgres, and a guarded route hands every message off while one of the
case's hand-writes is a stub (plan "Risks"). With every hand-write written there is no such
shortcut: the case needs its app, and runs.

**What a runner gets** (`FilledCase`): the case and its texts with every `{{pii:...}}`
placeholder filled by the frozen seed's generator, in memory only. A runner returns a
`scoring.CaseResult` of codes; it never logs or returns a filled text.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from vectapilot_api.guards.types import DecidedBy
from vectapilot_api.redteam.agent import run_agent
from vectapilot_api.redteam.cases import Case, Label
from vectapilot_api.redteam.limiter import run_limiter
from vectapilot_api.redteam.logs import run_logs
from vectapilot_api.redteam.masking import run_masking
from vectapilot_api.redteam.pii import PiiGenerator
from vectapilot_api.redteam.plan import (
    FRONTIER,
    HAND_WRITE_PROBES,
    Build,
    CasePath,
    Environment,
    path_of,
    pending_reason,
    unwritten,
    waiting_on,
)
from vectapilot_api.redteam.scoring import CaseResult, PendingHandWrite, Verdict, score
from vectapilot_api.redteam.search import run_search_leg

NOT_IMPLEMENTED: Final = NotImplementedError.__name__


@dataclass(frozen=True)
class FilledCase:
    """A case with its placeholders filled (module docstring). Never written anywhere."""

    case: Case
    input: str
    generator: PiiGenerator

    def text(self, template: str) -> str:
        """A fixture text (a scripted reply, a chunk, a tool field) with its placeholders filled
        with the same values as `input` (same case id, type and n)."""
        return self.generator.fill(self.case.id, template)

    def __repr__(self) -> str:
        # A filled text may hold generated personal data: a repr shows the id only.
        return f"FilledCase({self.case.id})"


def fill(case: Case, generator: PiiGenerator) -> FilledCase:
    return FilledCase(case=case, input=generator.fill(case.id, case.input), generator=generator)


@dataclass(frozen=True)
class RunEnv:
    """What a run hands every runner."""

    generator: PiiGenerator
    environment: Environment
    """`in_process` against the guard objects, or `database` through the served app."""
    app: object | None = None
    """The database environment's served app (in tests a module-scoped `TestClient`); None in
    process. Each runner narrows it to what it needs."""
    dataset_dir: Path | None = None
    """The dataset version's folder: the agent path builds its in-process world from it
    (`agent.world_of`: the fixture businesses' files and the guard folder)."""


class Runner(Protocol):
    """A path's runner (registered by the PR that builds its guard)."""

    def __call__(self, filled: FilledCase, env: RunEnv) -> CaseResult: ...


RUNNERS: Final[dict[CasePath, Runner]] = {}
"""Each built guard's runner, by path (module docstring)."""


def register(path: CasePath, runner: Runner) -> None:
    """Register `path`'s runner, once (a second registration is a programming error)."""
    if path in RUNNERS:
        raise ValueError(f"{path} already has a runner")
    RUNNERS[path] = runner


register(CasePath.MASKING, run_masking)  # M7 PR 3
register(CasePath.LIMITER, run_limiter)  # M7 PR 5
register(CasePath.AGENT, run_agent)  # M7 PR 6
register(CasePath.LOG, run_logs)  # M7 PR 6
register(CasePath.SEARCH_LEG, run_search_leg)  # M7 PR 6


def reached_a_stub(result: CaseResult) -> bool:
    return result.decided_by is DecidedBy.GUARD_ERROR and result.error_type == NOT_IMPLEMENTED


@dataclass(frozen=True)
class Scored:
    """`run_case`'s answer when no exception was raised."""

    result: CaseResult
    verdict: Verdict


def run_case(
    case: Case,
    label: Label,
    env: RunEnv,
    *,
    runners: Mapping[CasePath, Runner] = RUNNERS,
    probes: Mapping[Build, Callable[[], bool]] = HAND_WRITE_PROBES,
) -> Scored:
    """Run and score one case. Raises `PendingHandWrite` (its guard or a hand-write is not
    built), `NotScored` (a positive control failed) or `ExpectationMiss`."""
    path = path_of(case)
    runner = runners.get(path)
    if runner is None:
        raise PendingHandWrite(case.id, pending_reason(FRONTIER[path]))
    waiting = waiting_on(case, runners)
    if waiting is not None:
        raise PendingHandWrite(case.id, pending_reason(waiting))
    if env.environment == "database" and env.app is None:
        unbuilt = unwritten(case, probes)
        if unbuilt is not None:  # module docstring: pending without its served app
            raise PendingHandWrite(case.id, pending_reason(unbuilt))
    result = runner(fill(case, env.generator), env)
    if reached_a_stub(result):
        hand_write = unwritten(case, probes)
        if hand_write is not None:
            raise PendingHandWrite(case.id, pending_reason(hand_write))
    return Scored(result=result, verdict=score(case, result, label, env.environment))

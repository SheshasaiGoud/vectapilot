"""`run_case`: one red-team case through its guard, scored (M7 PR 2; scoring rules, "One scorer,
two callers").

pytest (`tests/redteam/test_cases.py`, test id `test_case[<id>]`) and `redteam run` both call
`run_case`, so a verdict means the same in CI and in a summary.

**The registry.** `RUNNERS` maps a `plan.CasePath` to the runner a later PR registers with
`register` (PR 3 masking, PR 5 the limiter, PR 6 the agent and the logs, PR 7 documents, PR 8 the
answer path). A case whose path has no runner raises `PendingHandWrite` with
`pending:<PR n>` before anything runs (plan, "Design points left to their PRs"): in PR 2 no guard
exists, so every case is pending. A runner that reports `guard_error` with `NotImplementedError`
reached a `[HAND-WRITE]` stub: pending on the first unwritten hand-write the case needs
(`plan.unwritten`). If none is unwritten, that hand-off is a plain miss.

**What a runner gets** (`FilledCase`): the case and its texts with every `{{pii:...}}`
placeholder filled by the frozen seed's generator, in memory only. A runner returns a
`scoring.CaseResult` of codes; it never logs or returns a filled text.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from vectapilot_api.guards.types import DecidedBy
from vectapilot_api.redteam.cases import Case, Label
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
)
from vectapilot_api.redteam.scoring import CaseResult, PendingHandWrite, Verdict, score

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


class Runner(Protocol):
    """A path's runner (registered by the PR that builds its guard)."""

    def __call__(self, filled: FilledCase, env: RunEnv) -> CaseResult: ...


RUNNERS: Final[dict[CasePath, Runner]] = {}
"""Empty in PR 2: no guard is built yet (module docstring)."""


def register(path: CasePath, runner: Runner) -> None:
    """Register `path`'s runner, once (a second registration is a programming error)."""
    if path in RUNNERS:
        raise ValueError(f"{path} already has a runner")
    RUNNERS[path] = runner


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
    result = runner(fill(case, env.generator), env)
    if reached_a_stub(result):
        hand_write = unwritten(case, probes)
        if hand_write is not None:
            raise PendingHandWrite(case.id, pending_reason(hand_write))
    return Scored(result=result, verdict=score(case, result, label, env.environment))

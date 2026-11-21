"""How each red-team case runs, and what CI expects of it while its guard is unbuilt (M7 PR 2;
scoring rules, "CI semantics"; plan, "Design points left to their PRs").

**Paths and the runner registry.** Every case belongs to one `CasePath` (`path_of`): the guard
object or route its attack goes through. A later PR registers a runner for a path
(`harness.RUNNERS`); until then every case on it is pending on that PR (`FRONTIER`):
- `masking` (E, in process against H2's `mask_pii`): PR 3;
- `limiter` (K on the limiter: k-002's cross-business limit and k-004's key scan, against real
  Redis on an isolated logical database, `redteam.limiter`): PR 5;
- `search_leg` (k-001, K on the search route): its control is RLS, built since M2, so it never
  waits for PR 8 [leo's PR 2 review], but its runner needs planted KB rows for the two sample
  businesses, removed afterwards as the owner, in the served suite that PR 6 reworks
  (`tests/redteam/served.py`'s note): PR 6 [proposal, moved from PR 5 by PR 5];
- `agent` (`/v1/agent` and its confirm route: A, B on the agent route, C, G and H on the agent,
  I, J, L, k-003): PR 6. B on the agent route (b-007, b-008) stays on its frozen route (the
  scoring rules: a router-on configuration with a stub router), but its poisoned chunk reaches
  the agent only through `agent_router = on`, a stub router and the agent's own `AnswerService`
  over the chunk screen, an answer step PR 8's chunk runner builds: until PR 8 registers the
  `answer` path it is pending on PR 8 (`waiting_on`) [kenji's PR 6 review, proposal];
- `log` (F's forced exceptions on every route, through the app's `RedactingJsonFormatter`):
  PR 6, with the route stand-ins and database fixtures its runner shares with the agent path.
  The formatter itself is built in PR 3 (it masks since H2 was written, 2026-11-20,
  `guards/redact.py`);
- `document` (D, `/v1/extractions`' page screens): PR 7;
- `answer` (`/v1/answer` and `/v1/kb/search` with `guard_answer = on` in tests): PR 8.

**Hand-writes.** A registered runner whose guard reaches a `[HAND-WRITE]` stub hands off with
`guard_error` and `NotImplementedError` (G3): the case is then pending on the first unwritten one
it needs (`needs`): a red-flag case only H1; E and F's logs only H2; D H1; K's limiter none; every
other agent or answer case H1, H2 and H3 (a guarded route hands off everything while one of them
is a stub, plan "Risks"). Whether a hand-write is written is probed (`HAND_WRITE_PROBES`): H1's
`match_terms`, since PR 3 H2's `mask_pii`, and since PR 6 H3's `check_reply`. A hand-write
without a probe counts as unwritten.

**Mark plans** (`mark_plan`): pending first (`pending:<PR n|Hn>` from `PENDING_REASONS`, a closed
list that also holds `pending:M5-H1` for the real-loop pass, D8 (i)), then the gate label: `hard`
(no mark), `known_gap` (`known_gap:<id>`), `soft` (its outcome recorded). The pytest helper
(`tests/redteam/marks.py`) turns a plan into the one mark; `redteam run` reports the same plan.

**Environments** (`environment_of`): the pure categories run in process against the guard
objects; F, J and K, every case on the confirm route (its control, confirm's 404, needs the route,
Postgres and the pending store: c-008), and the lowest-numbered case of every other surface (one
end-to-end case per surface) run through `TestClient` and the database [proposal: the lowest id,
so the choice is stable and visible].
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal

from vectapilot_api.guards.locales import locales
from vectapilot_api.guards.match import match_terms
from vectapilot_api.guards.output import OutputContext, OutputKind, check_reply
from vectapilot_api.guards.pii import mask_pii
from vectapilot_api.guards.types import Clean, Folded, TermHit, TermSet
from vectapilot_api.redteam.cases import Case, Category, Label, Route, Surface
from vectapilot_api.redteam.scoring import Environment as Environment


class CasePath(StrEnum):
    MASKING = "masking"
    LIMITER = "limiter"
    SEARCH_LEG = "search_leg"
    AGENT = "agent"
    LOG = "log"
    DOCUMENT = "document"
    ANSWER = "answer"


class Build(StrEnum):
    """What a pending case waits for: a plan PR, one of M7's hand-writes, or M5's H1 (the
    real-loop pass, D8 (i)). PR 4 (the term files) is not one: every path that reads terms is
    registered by a later PR (6, 7 or 8), and PR 4 lands first (plan, "Order of work")."""

    PR3 = "PR 3"
    PR5 = "PR 5"
    PR6 = "PR 6"
    PR7 = "PR 7"
    PR8 = "PR 8"
    H1 = "H1"
    H2 = "H2"
    H3 = "H3"
    M5_H1 = "M5-H1"


HandWrite = Literal[Build.H1, Build.H2, Build.H3]

PENDING_PREFIX: Final = "pending:"
KNOWN_GAP_PREFIX: Final = "known_gap:"
PENDING_REASONS: Final = frozenset(f"{PENDING_PREFIX}{build.value}" for build in Build)
"""The closed list of pending reasons (scoring rules, "CI semantics")."""
FINAL_PENDING: Final = frozenset({f"{PENDING_PREFIX}{Build.M5_H1.value}"})
"""What `redteam_gate.py --final` still allows (D8 (i)): only the real-loop pass, each listed as
"not measured through the real loop"."""

FRONTIER: Final[Mapping[CasePath, Build]] = MappingProxyType(
    {
        CasePath.MASKING: Build.PR3,
        CasePath.LIMITER: Build.PR5,
        CasePath.SEARCH_LEG: Build.PR6,
        CasePath.AGENT: Build.PR6,
        CasePath.LOG: Build.PR6,
        CasePath.DOCUMENT: Build.PR7,
        CasePath.ANSWER: Build.PR8,
    }
)
"""The PR that registers each path's runner (module docstring)."""


def path_of(case: Case) -> CasePath:
    if case.category is Category.E:
        return CasePath.MASKING
    if case.surface is Surface.LOG:
        return CasePath.LOG
    if case.category is Category.K and case.route is Route.SEARCH:
        return CasePath.SEARCH_LEG
    if case.category is Category.K and case.route is not Route.AGENT:
        return CasePath.LIMITER
    if case.surface is Surface.DOCUMENT or case.route in (Route.EXTRACTION, Route.SCAN):
        return CasePath.DOCUMENT
    if case.route in (Route.ANSWER, Route.SEARCH):
        return CasePath.ANSWER
    return CasePath.AGENT


def waiting_on(case: Case, registered: Iterable[CasePath]) -> Build | None:
    """The PR a case waits for although its path is registered (module docstring), or None:
    B on the agent route waits for PR 8's chunk runner, which registers the `answer` path."""
    agent_chunk = case.category is Category.B and path_of(case) is CasePath.AGENT
    if agent_chunk and CasePath.ANSWER not in set(registered):
        return Build.PR8
    return None


def needs(case: Case) -> tuple[HandWrite, ...]:
    """The hand-writes the case's guard reaches, in the order they are reported."""
    path = path_of(case)
    if path in (CasePath.MASKING, CasePath.LOG):
        return (Build.H2,)
    if path in (CasePath.LIMITER, CasePath.SEARCH_LEG):
        return ()
    if path is CasePath.DOCUMENT or case.expect in ("urgent_handoff", "high_handoff"):
        return (Build.H1,)
    return (Build.H1, Build.H2, Build.H3)


def _h1_written() -> bool:
    try:
        match_terms(Folded(""), TermSet(()))
    except NotImplementedError:
        return False
    return True


def _h2_written() -> bool:
    try:
        mask_pii(Clean(""), locales())
    except NotImplementedError:
        return False
    return True


def _no_hits(text: Folded, terms: TermSet) -> tuple[TermHit, ...]:
    return ()


def _h3_written() -> bool:
    context = OutputContext(
        business_type=None,
        health=True,
        message=Clean(""),
        kind=OutputKind.REPLY,
        cited=(),
        guidance=(),
        terms=TermSet(()),
        matcher=_no_hits,
        boundary_lines=(),
        locales=locales(),
    )
    try:
        check_reply("", context)
    except NotImplementedError:
        return False
    return True


HAND_WRITE_PROBES: Final[Mapping[Build, Callable[[], bool]]] = MappingProxyType(
    {Build.H1: _h1_written, Build.H2: _h2_written, Build.H3: _h3_written}
)
"""Whether each hand-write is written (module docstring): H1's and H2's since PR 2 and PR 3, H3's
since PR 6 (an empty reply with no terms, as each probe asks its stub the smallest question)."""


def unwritten(
    case: Case, probes: Mapping[Build, Callable[[], bool]] = HAND_WRITE_PROBES
) -> HandWrite | None:
    """The first hand-write the case needs that is not written yet, or None."""
    for hand_write in needs(case):
        probe = probes.get(hand_write)
        if probe is None or not probe():
            return hand_write
    return None


def pending_reason(build: Build) -> str:
    return f"{PENDING_PREFIX}{build.value}"


def known_gap_reason(case_id: str) -> str:
    return f"{KNOWN_GAP_PREFIX}{case_id}"


DATABASE_CATEGORIES: Final = frozenset({Category.F, Category.J, Category.K})


def end_to_end_ids(cases: Iterable[Case]) -> frozenset[str]:
    """The lowest-numbered case of every surface outside F, J and K (module docstring)."""
    first: dict[Surface, str] = {}
    for case in sorted(cases, key=lambda c: c.id):
        if case.category not in DATABASE_CATEGORIES:
            first.setdefault(case.surface, case.id)
    return frozenset(first.values())


def environment_of(case: Case, end_to_end: frozenset[str]) -> Environment:
    on_confirm = case.route is Route.CONFIRM
    if case.category in DATABASE_CATEGORIES or on_confirm or case.id in end_to_end:
        return "database"
    return "in_process"


MarkKind = Literal["hard", "known_gap", "soft", "pending"]


@dataclass(frozen=True)
class MarkPlan:
    """What CI expects of one case (module docstring)."""

    kind: MarkKind
    reason: str | None
    """`pending:<build>` or `known_gap:<id>`; None for hard and soft."""


def mark_plan(
    case: Case,
    label: Label,
    registered: Iterable[CasePath],
    probes: Mapping[Build, Callable[[], bool]] = HAND_WRITE_PROBES,
) -> MarkPlan:
    """The case's mark: pending on its path's PR or a hand-write, else its gate label."""
    path = path_of(case)
    registered = set(registered)
    if path not in registered:
        return MarkPlan("pending", pending_reason(FRONTIER[path]))
    waiting = waiting_on(case, registered)
    if waiting is not None:
        return MarkPlan("pending", pending_reason(waiting))
    hand_write = unwritten(case, probes)
    if hand_write is not None:
        return MarkPlan("pending", pending_reason(hand_write))
    if label == "known_gap":
        return MarkPlan("known_gap", known_gap_reason(case.id))
    return MarkPlan(label, None)

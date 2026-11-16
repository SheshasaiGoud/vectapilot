"""G13's fence: guards wrap the services; they never edit a measured one (M7 PR 1). Unit.

Four parts (the plan's G13); the first two are here, the third is the harnesses' own tests
passing unchanged, and the fourth is each `m7/*` PR's `git diff --stat main...HEAD` on the
fenced paths (an acceptance check, not a test):

1. **No fenced module imports `vectapilot_api.guards`**, in any spelling the W1 check sees
   (tests/actions/test_write_boundary.py's `reaches`): every module under `evalrun/`,
   `intenteval/`, `tooleval/`, `extracteval/` and `intents/`, `agent/service.py`,
   `agent/loop.py`, and `extraction/` but its route (`router.py`, where PR 6 wires the guard).
   `tooleval` imports `main` (`tooleval/runs.py:59`), so it loads `guards` transitively since PR 1
   (main imports the loader and the self-test): expected and harmless, because nothing guarded
   is constructed there (part 2).
2. **What the harnesses construct holds no guard:** `tooleval` builds `AgentService` with a
   `CappedGateway`, profiles whose executor is exactly `ToolExecutor`, and `NoAnswers`; `evalrun`
   builds `AnswerService` with exactly `SearchService` and its own `CappedGateway`. Exact types,
   never `isinstance`: a guard is a subclass (G13's seams), and a subclass is what must not
   appear.
3. **`main.build_action_services` keeps its source:** the harness takes the store and the confirm
   service from it, so a guard slipped in there would reach the eval. The sha256 of its
   `inspect.getsource` is pinned (equal to `main`'s at 5c001e1). M7 pins no file hash: shesha's
   H1s in fenced files land as planned.
"""

import asyncio
import hashlib
import inspect
from pathlib import Path
from typing import Any, Final, cast

import pytest

from tests.actions.test_write_boundary import SRC, module_of, python_files, reaches
from tests.agent.fakes import TOOLS
from tests.conftest import make_settings
from tests.evalrun.test_hybrid_harness import VECTOR_CONFIG
from tests.retrieval.standins import FakeDatabase
from tests.tooleval.helpers import parsed, scenario
from tests.tools.helpers import CORPUS_DIR
from vectapilot_api import main
from vectapilot_api.actions.confirm import ConfirmService
from vectapilot_api.actions.pending import PendingActionStore
from vectapilot_api.agent.profiles import business_types
from vectapilot_api.agent.responses import AgentRequest
from vectapilot_api.agent.service import AgentRun, AgentService
from vectapilot_api.answer.service import AnswerService
from vectapilot_api.db import Database
from vectapilot_api.evalrun import harness
from vectapilot_api.evalrun.budget import CappedGateway, CostCap
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.retrieval.search import SearchService
from vectapilot_api.tooleval import runs
from vectapilot_api.tooleval.harness import Io
from vectapilot_api.tooleval.runinfo import ReplyLine, ResultLine
from vectapilot_api.tooleval.runs import Outcome, Records
from vectapilot_api.tooleval.scenarios import Scenario
from vectapilot_api.tooleval.turns import Bench, LeakLog, NoAnswers
from vectapilot_api.tools.executor import ToolExecutor

GUARDS: Final = "vectapilot_api.guards"
FENCED_PACKAGES: Final = ("evalrun", "intenteval", "tooleval", "extracteval", "intents")
FENCED_FILES: Final = (SRC / "agent" / "service.py", SRC / "agent" / "loop.py")
EXTRACTION: Final = SRC / "extraction"
EXTRACTION_ROUTE: Final = EXTRACTION / "router.py"
BUILD_ACTION_SERVICES_SHA256: Final = (
    "681a69b87785f6d70696df61f06190a34892d728a78b868f3b352f9b4be79592"
)
TRACE: Final = "4bf92f3577b34da6a3ce929d0e0e4736"


def fenced_modules() -> list[Path]:
    packages = [SRC / name for name in FENCED_PACKAGES]
    extraction = [p for p in python_files(EXTRACTION) if p != EXTRACTION_ROUTE]
    return sorted([*python_files(*packages), *FENCED_FILES, *extraction])


# ── 1. The import fence ──────────────────────────────────────────────────────────────────────


def test_no_fenced_module_imports_the_guards() -> None:
    importers = [
        path.relative_to(SRC).as_posix()
        for path in fenced_modules()
        if reaches(path.read_text("utf-8"), module_of(path), GUARDS)
    ]

    assert importers == []


def test_the_fence_covers_the_measured_code_and_leaves_the_extraction_route_out() -> None:
    fenced = {path.relative_to(SRC).as_posix() for path in fenced_modules()}

    for expected in (
        "agent/service.py",
        "agent/loop.py",
        "intents/policy.py",
        "intents/fallback.py",
        "extraction/service.py",
        "extraction/routing.py",
        "evalrun/harness.py",
        "tooleval/runs.py",
        "intenteval/runs.py",
        "extracteval/runs.py",
    ):
        assert expected in fenced, expected
    assert "extraction/router.py" not in fenced  # PR 6 wires the guard there
    assert not any(name.startswith(("guards/", "answer/")) for name in fenced)


def test_red_proof_the_check_sees_a_real_importer() -> None:
    # main.py imports the guard loader and the self-test (M7 PR 1, G4/G14): the same check that
    # finds no fenced importer finds this one.
    assert reaches(inspect.getsource(main), "vectapilot_api.main", GUARDS)


@pytest.mark.parametrize(
    "source",
    [
        "from vectapilot_api.guards.match import match_terms",
        "from vectapilot_api import guards",
        "import vectapilot_api.guards.config as c",
        "from ..guards import normalize",
        "from ..guards.types import GuardRecord",
        "import vectapilot_api\nvectapilot_api.guards.normalize.fold('x')",
        'import importlib\nimportlib.import_module("vectapilot_api.guards.selftest")',
        '__import__("..guards.caps")',
    ],
)
def test_red_proof_every_spelling_of_a_guard_import_is_seen(source: str) -> None:
    assert reaches(source, "vectapilot_api.agent.loop", GUARDS), source


@pytest.mark.parametrize(
    "source",
    [
        "from vectapilot_api.tools.config import is_invisible",
        "from ..answer import guard_words",  # a name, not the package
        "guards = 1",
    ],
)
def test_the_check_is_about_the_package_not_the_word(source: str) -> None:
    assert not reaches(source, "vectapilot_api.agent.loop", GUARDS), source


# ── 2. What the harnesses construct ──────────────────────────────────────────────────────────


def test_tooleval_builds_the_agent_service_from_plain_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_Run.all` as a dry run does, up to the first turn, without a database: the role check and
    the sandbox reset are no-ops, and the scenario loop asks for one turn and stops. The service
    `respond` builds is recorded, never run."""
    assert vars(runs)["AgentService"] is AgentService  # the class it builds, before the patch
    built: list[tuple[tuple[Any, ...], dict[str, Any], AgentService]] = []
    benches: list[Bench] = []

    class Recorded:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            built.append((args, kwargs, AgentService(*args, **kwargs)))

        async def respond_unrouted(self, *args: Any, **kwargs: Any) -> AgentRun:
            return cast(AgentRun, None)

    async def role_checked(self: Database) -> None:
        return None

    async def no_reset(*args: Any) -> None:
        return None

    async def one_turn(self: runs._Run, bench: Bench, scenarios: Any, sink: Any) -> Outcome:
        benches.append(bench)
        request = AgentRequest(message="A synthetic test message.", customer_ref="cust-001")
        await bench.respond(scenarios[0], request, TRACE, LeakLog())
        return Outcome("complete")

    monkeypatch.setattr(runs, "AgentService", Recorded)
    monkeypatch.setattr(Database, "verify_role", role_checked)
    monkeypatch.setattr(runs, "reset", no_reset)
    monkeypatch.setattr(runs._Run, "_scenarios", one_turn)
    run = runs._Run(
        io=Io(),
        settings=make_settings(),
        catalog=load_catalog(),
        cap=CostCap(cap=None),
        records=Records(),
        provider=FakeProvider(),
        owner_url="postgresql+asyncpg://owner:not-a-real-secret@127.0.0.1:1/vectapilot",
        run_id="run-1",
        tools=TOOLS,
        types=business_types(TOOLS, CORPUS_DIR),
    )
    items: tuple[Scenario, ...] = (parsed(scenario("s-1")),)

    def sink(line: ResultLine, reply: ReplyLine | None) -> None:
        raise AssertionError("no line is written")

    assert asyncio.run(run.all(items, sink)) == Outcome("complete")

    [(args, kwargs, service)] = built
    [profiles] = args
    assert type(service) is AgentService
    assert type(kwargs["gateway"]) is CappedGateway
    assert type(kwargs["answers"]) is NoAnswers
    assert type(kwargs["store"]) is PendingActionStore
    assert profiles.keys() == TOOLS.keys()
    assert all(type(profile.executor) is ToolExecutor for profile in profiles.values())
    [bench] = benches
    assert type(bench.confirm) is ConfirmService


def test_evalrun_builds_the_answer_service_from_plain_parts() -> None:
    async def build() -> AnswerService:
        async with harness.wired(
            make_settings(),
            cast(Database, FakeDatabase()),  # never opened: nothing is searched here
            VECTOR_CONFIG,
            CostCap(cap=None),
            dry_run=True,
            records=lambda trace_id: None,
        ) as parts:
            return parts.answer

    answer = asyncio.run(build())

    assert type(answer) is AnswerService
    # Private attributes: `AnswerService` has no public getter, and this test must see exactly
    # what the harness handed it (as tests/evalrun/test_hybrid_harness.py reads `_search`).
    assert type(answer._search) is SearchService
    assert type(answer._gateway) is CappedGateway


# ── 3. build_action_services keeps its source ────────────────────────────────────────────────


def test_build_action_services_keeps_its_source() -> None:
    source = inspect.getsource(main.build_action_services)

    assert hashlib.sha256(source.encode("utf-8")).hexdigest() == BUILD_ACTION_SERVICES_SHA256

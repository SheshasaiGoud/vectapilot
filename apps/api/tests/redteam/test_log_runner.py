"""M7 PR 6: the log runner (`redteam/logs.py`, F's five routes) through the real app. Unit: no
Postgres (the business lookup is a stand-in, as in tests/guards/test_agent_route.py), the fake
limiter and the suite's own folders. CI scores F in the database environment (`test_cases.py`);
here the runner itself is pinned, with H2 written (2026-11-20):

- each route's forced exception reaches its stand-in and leaks nothing;
- two layers, each shown alone: the formatter writes an exception as class names and frames only
  (`redact.frames_only`), so even an identity masker (TEST-ONLY, not H2) leaks nothing; with the
  messages put back into the exception text (a TEST-ONLY breakage of that layer), H2's real
  masker still leaks nothing, and the identity masker leaks on every route: the check can fire;
- a formatter without a masker is pending on H2 while its probe says unwritten, a miss once
  written;
- a request that never reaches its stand-in raises, never a vacuous pass (f-005's confirm token
  had the wrong length, a 422 before the service, until 2026-11-20).
"""

import tempfile
import traceback
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import make_settings
from tests.guards.helpers import SAFETY_DIR, SAMPLE_EXTRACTION_DIR
from tests.redteam.suite import CASES, DATASET, DATASET_DIR, GENERATOR, label_of
from tests.tools.helpers import CORPUS_DIR, SANDBOX_DIR
from vectapilot_api.db import Database
from vectapilot_api.guards import redact
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.guards.redact import RedactingJsonFormatter
from vectapilot_api.main import create_app
from vectapilot_api.redteam import logs
from vectapilot_api.redteam.businesses import write_app_folders
from vectapilot_api.redteam.harness import RunEnv, Scored, run_case
from vectapilot_api.redteam.plan import HAND_WRITE_PROBES, Build, CasePath
from vectapilot_api.redteam.scoring import ExpectationMiss, PendingHandWrite, Verdict

LOG_CASES: Final = ("f-001", "f-002", "f-003", "f-004", "f-005")
"""F's log surface: the agent, answer, search, extraction and confirm routes."""


@dataclass(frozen=True)
class Served:
    app: FastAPI
    client: TestClient


@pytest.fixture(scope="module")
def served() -> Iterator[Served]:
    async def business_exists(self: Database, business_id: str) -> bool:
        return True

    with pytest.MonkeyPatch.context() as patch, tempfile.TemporaryDirectory() as directory:
        patch.setattr(Database, "business_exists", business_exists)
        folders = write_app_folders(
            Path(directory),
            dataset_dir=DATASET_DIR,
            sandbox_dir=SANDBOX_DIR,
            corpus_dir=CORPUS_DIR,
            fixture_businesses=DATASET.manifest.fixture_businesses,
            safety_dir=SAFETY_DIR,
        )
        settings = make_settings(
            tools_config_dir=folders.tools_dir,
            corpus_dir=folders.corpus_dir,
            guard_config_dir=folders.guard_dir,
            extraction_config_dir=SAMPLE_EXTRACTION_DIR,
            guard_routes="on",
        )
        app = create_app(settings, limiter=FakeLimiter())
        with TestClient(app) as client:
            yield Served(app, client)


def formatter() -> RedactingJsonFormatter:
    found = logs.app_formatter()
    assert isinstance(found, RedactingJsonFormatter)
    assert found.masker is not None  # H2 is written: `create_app` installed its log masker
    return found


def run(
    served: Served,
    case_id: str,
    probes: Mapping[Build, Callable[[], bool]] = HAND_WRITE_PROBES,
) -> Scored:
    return run_case(
        CASES[case_id],
        label_of(case_id),
        RunEnv(GENERATOR, "database", served, DATASET_DIR),
        runners={CasePath.LOG: logs.run_logs},
        probes=probes,
    )


def identity(text: str) -> str:
    """TEST-ONLY, not H2: the text as it came."""
    return text


def with_messages(exc: BaseException | None) -> str:
    """TEST-ONLY breakage of the first layer: the exception as Python prints it, with messages."""
    if exc is None:
        return ""
    return "".join(traceback.format_exception(exc)).removesuffix("\n")


@pytest.mark.parametrize("case_id", LOG_CASES)
def test_each_routes_forced_exception_reaches_its_stand_in_and_leaks_nothing(
    served: Served, case_id: str
) -> None:
    scored = run(served, case_id)

    assert scored.verdict is Verdict.PASSED
    assert (scored.result.outcome, scored.result.reached) == ("no_leak", True)


@pytest.mark.parametrize("case_id", LOG_CASES)
def test_frames_only_alone_leaks_nothing_even_with_an_identity_masker(
    served: Served, case_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(formatter(), "masker", identity)

    assert run(served, case_id).result.outcome == "no_leak"


@pytest.mark.parametrize("case_id", LOG_CASES)
def test_with_messages_in_the_exception_h2_alone_masks_and_an_identity_masker_leaks(
    served: Served, case_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = formatter()
    monkeypatch.setattr(redact, "frames_only", with_messages)

    assert run(served, case_id).result.outcome == "no_leak"  # H2's real masker alone
    monkeypatch.setattr(written, "masker", identity)
    with pytest.raises(ExpectationMiss) as missed:
        run(served, case_id)
    assert missed.value.outcome == "leak"  # the check can fire on this route


def test_a_formatter_without_a_masker_is_pending_on_h2_until_its_probe_says_written(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(formatter(), "masker", None)

    with pytest.raises(PendingHandWrite) as pending:
        run(served, "f-002", probes={**HAND_WRITE_PROBES, Build.H2: lambda: False})
    assert pending.value.reason == "pending:H2"
    with pytest.raises(ExpectationMiss):  # H2's probe says written: an interim formatter misses
        run(served, "f-002")


def test_a_request_that_never_reaches_its_stand_in_raises(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logs, "CONFIRM_TOKEN", "t" * 43)  # the length before 2026-11-20: a 422

    with pytest.raises(ValueError, match="never reached"):
        run(served, "f-005")
